"""Make any picture look like an Insta360 camera photo.

Modules
-------
jpeg      JPEG markers, quantisation tables, encoder, container re-assembly
exif      byte-level TIFF/EXIF editing (keeps the MakerNote intact)
meta      the proprietary trailer protobuf and the camera's media index
template  reading a real camera photo into a reusable :class:`Template`
build     the clone pipeline (render → assemble → verify)
camera    mount discovery, identity extraction, install, audit
cli       argparse and the four commands

The single-file entry point is ``insta360_clone.py`` in the project root.
"""

__version__ = "2.0.0"
