# The Pod Decision Service sidecar (ADR-0036): x86, like the Prebid Server task it runs in.
#
# scripts/_demand.py builds the context: site/ holds the fast-channels-pds package and its locked dependencies,
# installed for x86_64 with deterministic bytecode under /opt/pds (infra.bundling). The image only copies them onto
# the pinned Python base, so the same lock and sources give the same layers, and no build step runs Python.
FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

ENV PYTHONPATH=/opt/pds \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY site /opt/pds

# Not root; the task definition also makes the root filesystem read-only.
USER 1000:1000
EXPOSE 8090
ENTRYPOINT ["python", "-m", "fast_channels.pds"]
