"""Discovery: finding where a project lives, before reading it.

A source answers "what does the chain say about this token". This layer answers a different
question - "where on the open web is this project" - and it exists because the data sources
on Arc routinely ship a brand-new token with no socials at all, while X already has the
launch thread. What it returns is a candidate, never a fact: verification lives in identify.
"""
