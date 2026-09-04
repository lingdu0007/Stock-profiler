FROM caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d

ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY web/dist /srv

RUN find /etc/caddy/Caddyfile /srv -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

USER caddy
