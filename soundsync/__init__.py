"""
soundsync -- dual-system audio sync for Premiere Pro.

The command-line scripts in the parent folder are thin wrappers over this
package. Everything here is importable and reports progress through a Reporter
rather than printing, so the GUI and the CLI can drive the same code.

    matcher   stage 1: fingerprint + cross-correlate -> matches
    fcpxml    stage 2a: matches -> FCP7 XML of synced sequences
    clips     stage 2b: matches -> baked media files
    csvout    stage 2c: matches -> a pairing list you read yourself
    compat    what a given export setting will actually do to these sources
    pipeline  runs the stages together
"""

__version__ = "2.1"
