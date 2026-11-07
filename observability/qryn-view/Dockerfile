FROM caddy:2-alpine AS release

ADD --checksum=sha256:1c7dd99bd713c22b5fb2032d7f99f683593884b4de11cbaf527c5ba279f010a7 https://github.com/metrico/qryn-view/releases/download/v3.6.4/dist.zip /tmp/qryn-view.zip
RUN unzip -q /tmp/qryn-view.zip -d /srv/qryn-view

FROM caddy:2-alpine

COPY --from=release /srv/qryn-view /srv/qryn-view
COPY Caddyfile /etc/caddy/Caddyfile
