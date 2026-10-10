# The entry points, the same on a laptop and in CI:
#
#   make up     create the kind cluster, install Prometheus, Grafana and KEDA,
#               build the image and install the chart
#   make test   run the unit tests, with the coverage floor
#   make e2e    run the end-to-end tests against the kind cluster
#   make down   delete the kind cluster
#
# `up`, `e2e` and `down` need docker, kind, kubectl and helm on the PATH. CI
# installs the versions pinned in versions.env.
#
# Cluster safety. Every command below that talks to Kubernetes reads the
# kubeconfig file named here and no other, and stops unless that file's current
# context is the kind cluster `make up` created. Nothing in this file can reach
# a cluster this repository did not make, whatever kubectl points at elsewhere.

# The pinned tool, image and chart versions: KIND_NODE_IMAGE,
# KUBE_PROMETHEUS_STACK_CHART and KEDA_CHART_VERSION come from here.
include versions.env

CLUSTER_NAME := inference-on-kubernetes
# kind names the context it writes to the kubeconfig after the cluster.
KUBE_CONTEXT := kind-$(CLUSTER_NAME)
NAMESPACE := inference
RELEASE := inference
# Where Prometheus and Grafana run. The release has the chart's name, and the
# chart names its Services after the release: kube-prometheus-stack-prometheus
# and kube-prometheus-stack-grafana.
MONITORING_NAMESPACE := monitoring
MONITORING_RELEASE := kube-prometheus-stack
# Where KEDA, the autoscaler, runs, and where its chart is kept once it has
# been downloaded and checked.
KEDA_NAMESPACE := keda
KEDA_RELEASE := keda
KEDA_CHART := .cache/keda-$(KEDA_CHART_VERSION).tgz

# The commit being built and tested. It is the image's tag, the image reports
# it in its build-info metric, and the smoke test compares that with this.
GIT_SHA := $(shell git rev-parse HEAD)
IMAGE := inference-on-kubernetes:$(GIT_SHA)

# The kubeconfig: a file inside the repository, ignored by git. kind writes the
# cluster's address and credentials to it, and kubectl and helm find it through
# the environment. `override` makes this hold even against `make KUBECONFIG=...`.
override KUBECONFIG := $(CURDIR)/.kube/config
export KUBECONFIG

# Helm reads these variables before it reads the kubeconfig. A shell that has
# one of them set could send a release to another API server or namespace, so
# none of them is passed on to the commands below.
unexport HELM_KUBEAPISERVER HELM_KUBETOKEN HELM_KUBECONTEXT HELM_KUBEASUSER
unexport HELM_KUBEASGROUPS HELM_KUBECAFILE HELM_KUBEINSECURE_SKIP_TLS_VERIFY
unexport HELM_KUBETLS_SERVER_NAME HELM_NAMESPACE

# How long to wait for the cluster to come up and for a chart to become ready.
TIMEOUT := 5m

# The cluster has to exist before anything is installed in it, so targets run
# one at a time and in the order written, also under `make -j`.
.NOTPARALLEL:

.PHONY: help up test e2e down cluster nodes monitoring keda image deploy check-context diagnostics

help:
	@echo "make up     create the kind cluster, install Prometheus, Grafana and KEDA, build the image and install the chart"
	@echo "make test   run the unit tests, with the coverage floor"
	@echo "make e2e    run the end-to-end tests against the kind cluster"
	@echo "make down   delete the kind cluster"

# Monitoring and KEDA go in before the service: the service's chart creates a
# ServiceMonitor and a ScaledObject, kinds of object that exist only once
# those two charts have defined them.
up: cluster monitoring keda deploy

test:
	coverage run -m pytest
	coverage report

# The tests share one cluster and run in a fixed order, each building on what
# the ones before it showed, so the run stops at the first failure. Going on
# would mean waiting out every later test's patience on a cluster whose state
# is no longer known. The tests that put load on the service leave what they
# measured in reports/; `python -m e2e.summary` prints it as a table.
e2e: check-context
	GIT_SHA=$(GIT_SHA) pytest --verbose --exitfirst e2e

# kind talks to Docker, not to Kubernetes, and only about the cluster named
# here, so `down` and `cluster` need no context check. kind is told where the
# kubeconfig is all the same, because it is the one tool that writes to it.
# The file held nothing but this cluster, so it goes with it.
down:
	kind delete cluster --name $(CLUSTER_NAME) --kubeconfig "$(KUBECONFIG)"
	rm -f "$(KUBECONFIG)"

# Does nothing if the cluster exists, so `make up` can be repeated. To change
# the Kubernetes version, `make down` first.
cluster:
	kind get clusters | grep --quiet --line-regexp $(CLUSTER_NAME) || \
		kind create cluster --name $(CLUSTER_NAME) --config cluster/kind.yaml \
			--image $(KIND_NODE_IMAGE) --kubeconfig "$(KUBECONFIG)" --wait $(TIMEOUT)

# `kind create cluster --wait` waits for the control plane only. Nothing is
# installed before every node is Ready and cluster DNS is up.
nodes: check-context
	kubectl wait --for=condition=Ready nodes --all --timeout=$(TIMEOUT)
	kubectl --namespace kube-system rollout status deployment/coredns --timeout=$(TIMEOUT)

# Prometheus, the operator that configures it, Grafana and kube-state-metrics,
# from one chart; cluster/kube-prometheus-stack-values.yaml says what is
# switched off. `--wait` watches what the chart itself creates: the operator,
# Grafana and kube-state-metrics. The Prometheus pod is not among that. The
# chart creates an object of kind Prometheus and the operator makes the pod
# from it, which is what `kubectl wait` is there for.
monitoring: check-context nodes
	helm upgrade --install $(MONITORING_RELEASE) $(KUBE_PROMETHEUS_STACK_CHART) \
		--namespace $(MONITORING_NAMESPACE) --create-namespace \
		--values cluster/kube-prometheus-stack-values.yaml --wait --timeout $(TIMEOUT)
	kubectl --namespace $(MONITORING_NAMESPACE) wait prometheus --all \
		--for=condition=Available --timeout=$(TIMEOUT)
	kubectl --namespace $(MONITORING_NAMESPACE) get pods --output wide

# KEDA: the operator, which makes a HorizontalPodAutoscaler out of the
# service's ScaledObject, and the metrics server, which answers that
# autoscaler's question of what number to scale on. Kubernetes passes the
# question on through the API named below, so nothing is installed after this
# until that API answers. cluster/keda-values.yaml says what is switched off.
keda: check-context nodes $(KEDA_CHART)
	helm upgrade --install $(KEDA_RELEASE) $(KEDA_CHART) \
		--namespace $(KEDA_NAMESPACE) --create-namespace \
		--values cluster/keda-values.yaml --wait --timeout $(TIMEOUT)
	kubectl wait apiservice/v1beta1.external.metrics.k8s.io \
		--for=condition=Available --timeout=$(TIMEOUT)
	kubectl --namespace $(KEDA_NAMESPACE) get pods --output wide

# KEDA's chart, downloaded once and kept. It comes from a web server, which
# has no way of promising that the file is the one that was there yesterday,
# so shasum compares it with the SHA-256 in versions.env, and only a file that
# matches is given the name the target above looks for.
$(KEDA_CHART):
	mkdir -p $(@D)/unchecked
	helm pull keda --repo https://kedacore.github.io/charts --version $(KEDA_CHART_VERSION) \
		--destination $(@D)/unchecked
	echo "$(KEDA_CHART_SHA256)  $(@D)/unchecked/$(@F)" | shasum --algorithm 256 --check
	mv $(@D)/unchecked/$(@F) $@

image:
	docker build --build-arg GIT_SHA=$(GIT_SHA) --tag $(IMAGE) .

# Copies the image onto the nodes (there is no registry) and installs or
# upgrades the chart. `--wait` returns when the pods of the new version are
# Ready, or fails after the timeout. On a first install that is one pod: a
# new Deployment says nothing about replicas, and it is the autoscaler that
# makes them two. So the commands after it wait for the autoscaler to take
# charge: until KEDA has made it, until it has been given its number, which
# takes as long as Prometheus needs to begin scraping the new pods, and until
# the pods it wants are Ready.
deploy: check-context nodes image
	kind load docker-image $(IMAGE) --name $(CLUSTER_NAME)
	kubectl apply --filename cluster/namespace.yaml
	helm upgrade --install $(RELEASE) chart --namespace $(NAMESPACE) \
		--values chart/values-kind.yaml --set-string image.tag=$(GIT_SHA) \
		--wait --timeout $(TIMEOUT)
	kubectl --namespace $(NAMESPACE) wait horizontalpodautoscaler/$(RELEASE) \
		--for=create --timeout=$(TIMEOUT)
	kubectl --namespace $(NAMESPACE) wait horizontalpodautoscaler/$(RELEASE) \
		--for=condition=ScalingActive --timeout=$(TIMEOUT)
	kubectl --namespace $(NAMESPACE) rollout status deployment/$(RELEASE) --timeout=$(TIMEOUT)
	kubectl --namespace $(NAMESPACE) get pods --output wide

# The guard every target that uses kubectl or helm depends on. With no cluster
# there is no kubeconfig and no current context, so this refuses then too.
check-context:
	@context=$$(kubectl config current-context 2>/dev/null); \
	if [ "$$context" != "$(KUBE_CONTEXT)" ]; then \
		echo "Refusing to continue: the current context in $(KUBECONFIG) is '$$context', not '$(KUBE_CONTEXT)'." >&2; \
		echo "Create the cluster with 'make up'. If one is left over from before, 'make down' first." >&2; \
		exit 1; \
	fi
	@echo "Using context $(KUBE_CONTEXT) from $(KUBECONFIG)"

# What CI keeps of every run, passed or failed: the events, the state of the
# pods, the service's logs, the load generator's and KEDA's, Prometheus's list
# of what it scrapes and of its rules, with how each is going (asked for
# through the API server, which passes the request on to the Service), and
# kind's own dump of each node with the kubelet's and every container's log.
# The leading "-" lets one command fail without stopping the ones after it.
diagnostics: check-context
	mkdir -p diagnostics
	-kubectl get events --all-namespaces --sort-by=.metadata.creationTimestamp > diagnostics/events.txt
	-kubectl get pods --all-namespaces --output wide > diagnostics/pods.txt
	-kubectl --namespace $(NAMESPACE) describe all,poddisruptionbudgets,prometheusrules,scaledobjects > diagnostics/describe.txt
	-kubectl --namespace $(MONITORING_NAMESPACE) describe all > diagnostics/describe-monitoring.txt
	-kubectl --namespace $(NAMESPACE) logs --selector app.kubernetes.io/instance=$(RELEASE) \
		--all-containers --prefix --tail=-1 > diagnostics/logs.txt
	-kubectl --namespace default logs --selector app.kubernetes.io/name=loadgen \
		--prefix --tail=-1 > diagnostics/loadgen.txt
	-kubectl --namespace $(KEDA_NAMESPACE) logs --selector app.kubernetes.io/instance=$(KEDA_RELEASE) \
		--all-containers --prefix --tail=-1 > diagnostics/keda.txt
	-kubectl get --raw /api/v1/namespaces/$(MONITORING_NAMESPACE)/services/$(MONITORING_RELEASE)-prometheus:9090/proxy/api/v1/targets \
		> diagnostics/prometheus-targets.json
	-kubectl get --raw /api/v1/namespaces/$(MONITORING_NAMESPACE)/services/$(MONITORING_RELEASE)-prometheus:9090/proxy/api/v1/rules \
		> diagnostics/prometheus-rules.json
	-kind export logs diagnostics/kind --name $(CLUSTER_NAME)
