"""Durable collection of Numerai's scoring of our submissions.

We have submitted the percentile rank of convergence_score to Numerai Signals
weekly since at least round 1253 (2026-04-30) and never once read back how it
scored. submit_numerai.py records that a submission was accepted; nothing
recorded the result.

collect.py fixes that permanently: it pulls the full round history every run
and upserts it into a durable CSV, so the record survives a failed fetch, a
renamed API field, or a missed day.
"""
