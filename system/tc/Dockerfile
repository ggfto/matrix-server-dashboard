# Tiny image with `tc`, used to shape another container's network
# (docker run --network container:<name> --cap-add NET_ADMIN matrix-tc ...).
FROM alpine:3.20
RUN apk add --no-cache iproute2-tc
