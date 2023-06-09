# -*- coding: utf-8 -*-

#
# Copyright (c) 2019 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Functions and classes used to simplify the execution of commands.
"""

import array
import fcntl
import io
import os
import os.path
import selectors
import subprocess
import termios


class LineBuffer:
    """
    Accumulates the data passed via the `write` methods till it finds complete
    lines. Then it passes those complete lines to the stream passed in the
    constructor, or to the log if there is no stream.
    """
    def __init__(self, log, stream=None):
        """
        Creates a new buffer that will use the given stream to write lines. If
        the stream isn't provided then it will write the lines to the log.
        """
        # Save the log and the stream:
        self._log = log
        self._stream = stream

        # Create the buffer:
        self._buffer = io.BytesIO()

    def _write_line(self, line):
        """
        Writes the given line to the stream, or to the log if there is no
        stream.
        """
        if self._stream is not None:
            self._stream.write(line)
            self._stream.write(b"\n")
        else:
            self._log.info(line.decode("utf-8"))

    def write(self, data):
        """
        Processes the given data.
        """
        # Process all the new line characters that appear in the data:
        while True:
            index = data.find(b"\n")
            if index == -1:
                break
            line = data[0:index]
            data = data[index+1:]
            pending = self._buffer.getvalue()
            if pending:
                line = pending + line
                self._buffer.truncate(0)
                self._buffer.seek(0, io.SEEK_SET)
            self._write_line(line)

        # If there is any remaining data then add it to the buffer:
        if data:
            self._buffer.write(data)

    def flush(self):
        """
        Processes the remaining data that is not a complete line. This is
        intended to process the data at the end of the stream that may not end
        with a line separator.
        """
        line = self._buffer.getvalue()
        if line:
            self._write_line(line)
            self._buffer.truncate(0)
            self._buffer.seek(0, io.SEEK_SET)


class Command:
    """
    Simplifies the execution of commands.
    """
    def __init__(self, log, command=[], env=None, cwd=None):
        """
        Creates a new object that simplifies running commands.

        For a simple binary set `command` to 1-element array e.g. ["ls"],
        but can also include common initial arguments e.g. ["ls", "-l"].
        """
        self._log = log
        self._command = command
        self._env = env
        self._cwd = cwd

    def _args(self, args):
        """
        Calculates the complete list of arguments for this command, adding the
        binary and initial args.
        """
        return self._command + args

    def _available(self, fd):
        """
        Returns the amount of bytes that are available for read in the given
        file descriptor.
        """
        holder = array.array("i", [0])
        fcntl.ioctl(fd, termios.FIONREAD, holder)
        return holder[0]

    def _process(self, fd, buf):
        """
        Processes the data available in the given file descriptor.
        """
        count = self._available(fd)
        if count > 0:
            data = os.read(fd, count)
            buf.write(data)

    def _run(self, args, stdin, stdout, stderr):
        """
        Executes the command with the given arguments and environment and
        current working directory given in the constructor and returns its
        exit code.

        The `stdin`, `stdout` and `stderr` parameters are mandatory and should
        contain either `None` or the file objects that will be used as the
        standard input, output and errors streams of the process.
        """
        # Log the complete command that will be executed:
        self._log.info(f"Running command {args}")

        # Create the pipes that we will use to read the output and errors
        # generated by the command:
        out_reader, out_writer = os.pipe()
        err_reader, err_writer = os.pipe()

        # Create the buffers where we will store the output and generated by
        # the command till we have complete lines that can then be processed:
        out_buffer = LineBuffer(log=self._log, stream=stdout)
        err_buffer = LineBuffer(log=self._log, stream=stderr)

        # Create the selector that we will use to be notified when there is
        # data to read from the pipes:
        selector = selectors.DefaultSelector()
        selector.register(out_reader, selectors.EVENT_READ, data=out_buffer)
        selector.register(err_reader, selectors.EVENT_READ, data=err_buffer)

        # Start the process:
        process = subprocess.Popen(
            args=args,
            env=self._env,
            cwd=self._cwd,
            stdin=stdin,
            stdout=out_writer,
            stderr=err_writer,
        )

        # Wait till the process finishes, and meanwhile process the data coming
        # from the command:
        while True:
            events = selector.select(timeout=0.1)
            for key, _ in events:
                self._process(key.fileobj, key.data)
            if process.poll() is not None:
                break

        # We no longer need the selector:
        selector.close()

        # We need to actually wait for the process, to avoid a zombie:
        result = process.wait()

        # After the process has finished there may still be some remaining data
        # in the pipes that was written in the interval between we check for
        # available data and we check if the process finished. We need to
        # process that data now.
        self._process(out_reader, out_buffer)
        self._process(err_reader, err_buffer)

        # Now that all the data has been processed we can close the pipes:
        os.close(out_writer)
        os.close(out_reader)
        os.close(err_writer)
        os.close(err_reader)

        # Flush the buffers to complete processing of potential last lines that
        # don't need with a new line character:
        out_buffer.flush()
        err_buffer.flush()

        # Return the the exit code of the command:
        return result

    def run(self, args=[], stdin=None, stdout=None, stderr=None):
        """
        Executes the command with the given arguments and environment and
        current working directory given in the constructor and returns its
        exit code.

        The optional `stdin`, `stdout` and `stderr` parameters should be the
        names of the files that will be used as the standard input, output and
        errors stream of the process.
        """
        # Calculate the complete list of arguments:
        args = self._args(args)

        # Open the input and output files and remember to close them regardless of any exception
        # that may be raised while running the process:
        try:
            if stdin is not None:
                stdin = open(stdin, "rb")
            if stdout is not None:
                stdout = open(stdout, "wb")
            if stderr is not None:
                stderr = open(stderr, "wb")
            return self._run(args, stdin, stdout, stderr)
        finally:
            if stdin is not None:
                stdin.close()
            if stdout is not None:
                stdout.close()
            if stderr is not None:
                stderr.close()

    def check(self, args=[], stdin=None, stdout=None, stderr=None):
        """
        Executes the command with the given arguments and environment and
        current working directory given in the constructor and raises an
        exception if it finishes with an exit code other than zero.

        The optional `stdin`, `stdout` and `stderr` parameters should be the
        names of the files that will be used as the standard input, output and
        errors stream of the process.
        """
        result = self.run(
            args=args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        if result != 0:
            raise Exception(f"Command finished with exit code {result}")

    def eval(self, args=[]):
        """
        Executes the command with the given arguments and environment and
        working directory given in the constructor. If the command finishes
        with exit code zero then it returns the standard output generated
        by the process. If the command finishes with an exit code other than
        zero it raises an exception.
        """
        # Calculate the complete command line:
        _args = self._args(args)

        # Evaluate the command:
        command = " ".join(_args)
        self._log.info(f"Evaluating command '{command}'")
        process = subprocess.Popen(
            args=_args,
            env=self._env,
            cwd=self._cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        output, _ = process.communicate()
        result = process.wait()
        if result != 0:
            raise Exception(
                f"Command '{command}' finished with exit code {result}"
            )
        return output.decode("utf-8")
