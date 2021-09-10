#!/usr/bin/python3

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from jinja2 import Template
from kubernetes import client, config

import requests

import os
import subprocess
import time
import yaml

def get_configmap(config_map_name, k8s_client, retry=False):
    with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as ns_file:
        target_namespace = ns_file.read()

    print("Loading ConfigMap {0} in namspace {1}".format(config_map_name, target_namespace))
    v1 = client.CoreV1Api(k8s_client)
    cm = v1.read_namespaced_config_map(name=config_map_name, namespace=target_namespace)
    return cm.data

def process_tls_certificates(backend_ca_data=None):
    LDAP_PROXY_TLS_SECRET_PATH = os.environ.get('LDAP_PROXY_TLS_SECRET_PATH', '/etc/tls/private')
    with open(os.path.join(LDAP_PROXY_TLS_SECRET_PATH, 'tls.crt')) as cert_file:
        cert_data = cert_file.read()
    with open(os.path.join(LDAP_PROXY_TLS_SECRET_PATH, 'tls.key')) as key_file:
        key_data = key_file.read()
    with open('/var/lib/haproxy/haproxy.pem', 'w+') as haproxy_pem_file:
        haproxy_pem_file.write("{0}\n{1}".format(cert_data, key_data))

    if backend_ca_data:
        with open('/var/lib/haproxy/backend-ca.pem', 'w+') as backend_ca_file:
            backend_ca_file.write(backend_ca_data)

def template_haproxy_config(config_map_data):
    with open('/templates/haproxy.cfg.j2') as template_file:
        jinja_template = Template(template_file.read())
    with open('/var/lib/haproxy/haproxy.cfg', 'w+') as haproxy_cfg:
        haproxy_cfg.write(jinja_template.render(config=config_map_data))

def update_ca_configmap(k8s_client):
    source_cm_name = os.environ.get('LDAP_PROXY_SERVING_SOURCE_CONFIGMAP', 'ldap-proxy-service-ca')
    source_cm_namespace = os.environ.get('LDAP_PROXY_NAMESPACE', 'ldap-proxy')
    dest_cm_name = os.environ.get('LDAP_PROXY_SERVING_DEST_CONFIGMAP', 'ldap-proxy-ca')
    dest_cm_namespace = 'openshift-config'
    v1 = client.CoreV1Api(k8s_client)
    raw_source = v1.read_namespaced_config_map(name=source_cm_name, namespace=source_cm_namespace)
    source_cm_data = raw_source.data
    source_cm_version = raw_source.metadata.resource_version
    raw_dest = v1.read_namespaced_config_map(name=dest_cm_name, namespace=dest_cm_namespace)
    dest_cm_version = 0
    if raw_dest.data:
        if 'source_resource_version' in raw_dest.data:
            dest_cm_version = raw_dest.data['source_resource_version']
    print("Got version {0} from destination CM".format(dest_cm_version))
    print("Got version {0} from source CM".format(source_cm_version))
    if dest_cm_version != source_cm_version:
        print("ldap-proxy-ca ConfigMap needs updating")
        if not raw_dest.data:
            raw_dest.data = {}
        raw_dest.data['ca.crt'] = source_cm_data['service-ca.crt']
        raw_dest.data['source_resource_version'] = source_cm_version
        v1.replace_namespaced_config_map(name=dest_cm_name, namespace=dest_cm_namespace, body=raw_dest)

def run_haproxy():
    haproxy_args = ['/sbin/haproxy', '-f', '/var/lib/haproxy/haproxy.cfg']
    subprocess.run(haproxy_args, stderr=subprocess.STDOUT)

if __name__ == '__main__':
    if os.environ.get('USE_KUBECONFIG', False):
        print("Using local kubeconfig")
        config.load_kube_config()
        k8s_api_client = config.new_client_from_config()
    else:
        print("Using incluster kubernetes credentials")
        configuration = client.Configuration()
        config.load_incluster_config(configuration)
        k8s_api_client = client.ApiClient(configuration)

    if os.environ.get('UPDATE_CA_CONFIGMAP', True):
        print("Updating the serving CA cofigmap if needed")
        update_ca_configmap(k8s_client=k8s_api_client)
    else:
        print("UPDATE_CA_CONFIMAP disabled, skipping service CA configmap update")

    print("Loading backend CA config") 
    PROXY_CONFIG_MAP_NAME = os.environ.get('PROXY_CONFIG_MAP_NAME', 'ldap-proxy-config')
    CA_CONFIG_MAP_NAME = os.environ.get('CA_CONFIG_MAP_NAME', 'ldap-proxy-ca')
    backend_ca_configmap = get_configmap(CA_CONFIG_MAP_NAME, k8s_client=k8s_api_client, retry=False)
    if backend_ca_configmap:
        backend_ca_data = backend_ca_configmap.get('ca-bundle.crt', None)
    else:
        backend_ca_data = None

    print("Processing TLS certificates")
    # Render secret-based TLS certificate and backend CA into HAproxy format
    process_tls_certificates(backend_ca_data)

    print("Generating HAproxy config")
    #Render HAproxy Configuration
    config_map_raw = get_configmap(PROXY_CONFIG_MAP_NAME, k8s_client=k8s_api_client, retry=True)
    config_map_data = yaml.load(config_map_raw.get('config.yaml', '{}'))
    template_haproxy_config(config_map_data)

    print("Starting HAproxy")
    # Run HAproxy in foreground
    run_haproxy()
