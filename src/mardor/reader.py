# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""MAR reading support.

This module provides the MarReader class which is used to read, extract, and
verify MAR files.
"""

import os
import tempfile
from enum import Enum

from cryptography.exceptions import InvalidSignature

from mardor.format import mar
from mardor.signing import SigningAlgo
from mardor.signing import get_signature_data
from mardor.signing import make_verifier_v1
from mardor.signing import make_verifier_v2
from mardor.utils import auto_decompress_stream
from mardor.utils import bz2_decompress_stream
from mardor.utils import file_iter
from mardor.utils import mkdir
from mardor.utils import safejoin
from mardor.utils import takeexactly
from mardor.utils import write_to_file
from mardor.utils import xz_decompress_stream


class Decompression(Enum):
    """
    Enum representing different decompression options.

    none: don't decompress
    auto: automatically decompress depending on specific format
    bz2: decompress using bz2
    xz: decompress using xz
    """

    none = None
    auto = 1
    bz2 = 2
    xz = 3


class MarReader(object):
    """Support for reading, extracting, and verifying MAR files.

    Example::
        with MarReader(open('test.mar', 'rb')) as m:
            m.extract('/tmp/extracted')
    """

    def __init__(self, fileobj):
        """Initialize a new MarReader object.

        Note:
            Files should always be opened in binary mode.

        Args:
            fileobj (file object): A file-like object open in read mode where
                the MAR data will be read from. This object must also be
                seekable (i.e.  support .seek() and .tell()).
        """
        self._raw_fileobj = fileobj
        self._decompressed_fileobj = None

        self.mardata = mar.parse_stream(self._raw_fileobj)
        self.is_compressed = self.mardata.is_compressed

    @property
    def fileobj(self):
        if not self.is_compressed:
            return self._raw_fileobj

        if not self._decompressed_fileobj:
            self._decompressed_fileobj = self.decompress()
        return self._decompressed_fileobj

    def decompress(self):
        """Decompress the compressed data section of the mar file
        Return a fileobject pointing to the decompressed data.
        """
        dst = tempfile.TemporaryFile()
        dst.seek(self.mardata.data_offset)

        self._raw_fileobj.seek(self.mardata.data_offset)

        stream = takeexactly(file_iter(self._raw_fileobj), self.mardata.data_length)
        stream = xz_decompress_stream(stream)

        write_to_file(stream, dst)

        dst.seek(0)
        return dst

    def __enter__(self):
        """Support the context manager protocol."""
        return self

    def __exit__(self, type_, value, tb):
        """Support the context manager protocol."""
        pass

    def extract_entry(self, e, decompress=Decompression.auto):
        """Yield blocks of data for this entry from this MAR file.

        Args:
            e (:obj:`mardor.format.index_entry`): An index_entry object that
                refers to this file's size and offset inside the MAR file.
            path (str): Where on disk to extract this file to.
            decompress (obj, optional): Controls whether files are decompressed
                when extracted. Must be an instance of Decompression. defaults
                to Decompression.auto

        Yields:
            Blocks of data for `e`
        """
        self.fileobj.seek(e.offset)
        stream = file_iter(self.fileobj)
        stream = takeexactly(stream, e.size)
        if decompress == Decompression.auto:
            stream = auto_decompress_stream(stream)
        elif decompress == Decompression.bz2:
            stream = bz2_decompress_stream(stream)
        elif decompress == Decompression.xz:
            # Nothing to do here since we've already decompressed the XZ chunk
            pass

        for block in stream:
            yield block

    def extract(self, destdir, decompress=Decompression.auto):
        """Extract the entire MAR file into a directory.

        Args:
            destdir (str): A local directory on disk into which the contents of
                this MAR file will be extracted. Required parent directories
                will be created as necessary.
            decompress (obj, optional): Controls whether files are decompressed
                when extracted. Must be one of 'auto' or None. Defaults to
                'auto'.
        """
        for e in self.mardata.index.entries:
            name = e.name
            entry_path = safejoin(destdir, name)
            entry_dir = os.path.dirname(entry_path)
            mkdir(entry_dir)
            with open(entry_path, 'wb') as f:
                write_to_file(self.extract_entry(e, decompress), f)

    def verify(self, verify_key):
        """Verify that this MAR file has a valid signature.

        Args:
            verify_key (str): PEM formatted public key

        Returns:
            True if the MAR file's signature matches its contents
            False otherwise; this includes cases where there is no signature.
        """
        if not self.mardata.signatures or not self.mardata.signatures.sigs:
            # This MAR file can't be verified since it has no signatures
            return False

        verifiers = []
        for sig in self.mardata.signatures.sigs:
            if sig.algorithm_id == SigningAlgo.SHA1:
                verifier = make_verifier_v1(verify_key, sig.signature)
                verifiers.append(verifier)
            elif sig.algorithm_id == SigningAlgo.SHA384:
                verifier = make_verifier_v2(verify_key, sig.signature)
                verifiers.append(verifier)
            else:
                raise ValueError('Unsupported algorithm ({})'.format(sig.algorithm_id))

        assert len(verifiers) == len(self.mardata.signatures.sigs)

        for block in get_signature_data(self.fileobj,
                                        self.mardata.signatures.filesize):
            [v.update(block) for v in verifiers]

        for v in verifiers:
            try:
                v.verify()
            except InvalidSignature:
                return False
        else:
            return True
