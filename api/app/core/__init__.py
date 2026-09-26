"""Pure domain logic: filters, scoring, funnel arithmetic.

No IO, no clock, no network in this package. Everything here is a pure function so
that "change one number, recompute, see who moves" is a repeatable answer.
"""
