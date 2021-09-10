FROM registry.access.redhat.com/ubi8/ubi-minimal:latest

RUN microdnf install -y haproxy python38 python38-jinja2 python38-pyyaml python38-requests && chown haproxy:root /var/lib/haproxy && pip3 install kubernetes && chmod 770 /var/lib/haproxy

COPY entrypoint.py /entrypoint.py
COPY templates /templates

EXPOSE 636

CMD /entrypoint.py
