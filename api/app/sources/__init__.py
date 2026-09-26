"""Data-source adapters.

One file per source, and sources never know about each other: the pipeline is what
combines them. Endpoint shapes are filled in by the step-0 probe (SOURCES.md, this directory);
until then adapters declare their intent and raise rather than inventing fields.
"""
