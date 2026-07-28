# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# Built once by the publisher and distributed by digest. Customers never build
# this image, so no customer deployment depends on the Alpine or rclone
# download endpoints being reachable.
FROM alpine:3.22.1@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1 AS downloader

ARG TARGETARCH=amd64
ARG RCLONE_VERSION=1.74.4

RUN apk add --no-cache ca-certificates curl unzip \
    && case "${TARGETARCH}" in \
         amd64) archive_arch="amd64"; archive_sha="fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d" ;; \
         arm64) archive_arch="arm64"; archive_sha="97685285c9ad6a0cf17d5844115d2a67245af6444db672187074bd9c358de419" ;; \
         *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && archive="rclone-v${RCLONE_VERSION}-linux-${archive_arch}.zip" \
    && curl --fail --show-error --silent --location \
         "https://downloads.rclone.org/v${RCLONE_VERSION}/${archive}" \
         --output "/tmp/${archive}" \
    && echo "${archive_sha}  /tmp/${archive}" | sha256sum -c - \
    && unzip -q "/tmp/${archive}" -d /tmp/rclone \
    && install -m 0755 "/tmp/rclone/rclone-v${RCLONE_VERSION}-linux-${archive_arch}/rclone" /usr/local/bin/rclone

FROM alpine:3.22.1@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1

LABEL org.opencontainers.image.title="azure-sharepoint-copy" \
      org.opencontainers.image.description="One-way scheduled copy from Azure Files or ADLS Gen2 to a SharePoint document library." \
      org.opencontainers.image.licenses="MIT"

COPY --from=downloader /usr/local/bin/rclone /usr/local/bin/rclone
COPY app/transfer.sh /app/transfer.sh

# Deliberately unpinned patch versions: this image is rebuilt and re-pinned by
# digest on each release, so pinning an exact Alpine package revision here only
# breaks the build when upstream ships a rebuild.
RUN apk add --no-cache ca-certificates jq curl \
    && chmod 0555 /app/transfer.sh \
    && mkdir -p /work \
    && chown 65532:65532 /work

WORKDIR /work
USER 65532:65532
ENTRYPOINT ["/app/transfer.sh"]
