# The Prebid Sales Agent with the FAST Channels bridge (modules.selling, docs/selling.md).
#
# BASE is the seller built from its own Dockerfile at the pinned commit, unmodified (build-image.sh). This layer adds
# files and one package and changes none of the seller's: every result is the stock seller's behaviour.
ARG BASE
FROM ${BASE}
USER root
# The bridge invokes the sync function; the seller's lock has no AWS SDK (P0-13).
RUN /usr/local/bin/python -m pip --python /opt/venv/bin/python install --no-cache-dir "boto3==1.43.121"
COPY --chown=app:app fast/ /app/fast/
USER app:app
ENTRYPOINT ["/opt/venv/bin/python", "/app/fast/entrypoint.py"]
CMD ["serve"]
