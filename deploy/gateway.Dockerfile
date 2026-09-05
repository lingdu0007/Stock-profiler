FROM caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d

ARG SOURCE_SHA=0000000000000000000000000000000000000000
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

LABEL org.opencontainers.image.revision=${SOURCE_SHA}

COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY web/dist /srv

RUN addgroup -S -g 10001 stock-profiler \
    && adduser -S -D -H -u 10001 -G stock-profiler stock-profiler \
    && mkdir -p /config/caddy /data/caddy \
    && chown -R stock-profiler:stock-profiler /config /data /etc/caddy /srv \
    && find /etc/caddy/Caddyfile /srv -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

ENV XDG_CONFIG_HOME=/config
ENV XDG_DATA_HOME=/data

USER 10001:10001
