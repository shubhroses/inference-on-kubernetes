# The entry points, the same on a laptop and in CI:
#
#   make up     create the kind cluster, build the image and install the chart
#   make test   run the unit tests, with the coverage floor
#   make down   delete the kind cluster
#
# `up` and `down` need docker, kind, kubectl and helm on the PATH. CI installs
# the versions pinned in versions.env.
#
# Cluster safety. Every command below that talks to Kubernetes reads the
# kubeconfig file named here and no other, and stops unless that file's current
# context is the kind cluster `make up` created. Nothing in this file can reach
# a cluster this repository did not make, whatever kubectl points at elsewhere.

# The pinned tool and image versions, KIND_NODE_IMAGE among them.
include versions.env

CLUSTER_NAME := inference-on-kubernetes
# kind names the context it writes to the kubeconfig after the cluster.
KUBE_CONTEXT := kind-$(CLUSTER_NAME)
NAMESPACE := inference
RELEASE := inference

# The commit being built. It is the image's tag, and the image reports it in
# its build-info metric.
GIT_SHA := $(shell git rev-parse HEAD)
IMAGE := inference-on-kubernetes:$(GIT_SHA)

# The kubeconfig: a file inside the repository, ignored by git. kind writes the
# cluster's address and credentials to it, and kubectl and helm find it through
# the environment. `override` makes this hold even against `make KUBECONFIG=...`.
override KUBECONFIG := $(CURDIR)/.kube/config
export KUBECONFIG

# How long to wait for the cluster to come up and for the chart to become ready.
TIMEOUT := 5m

# The cluster has to exist before anything is installed in it, so targets run
# one at a time and in the order written, also under `make -j`.
.NOTPARALLEL:

.PHONY: help up test down cluster image deploy check-context diagnostics

help:
	@echo "make up     create the kind cluster, build the image and install the chart"
	@echo "make test   run the unit tests, with the coverage floor"
	@echo "make down   delete the kind cluster"

up: cluster deploy

test:
	coverage run -m pytest
	coverage report

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

image:
	docker build --build-arg GIT_SHA=$(GIT_SHA) --tag $(IMAGE) .

# Waits for every node and for cluster DNS, copies the image onto the nodes
# (there is no registry), and installs or upgrades the chart. `--wait` returns
# when every pod of the new version is Ready, or fails after the timeout.
deploy: check-context image
	kubectl wait --for=condition=Ready nodes --all --timeout=$(TIMEOUT)
	kubectl --namespace kube-system rollout status deployment/coredns --timeout=$(TIMEOUT)
	kind load docker-image $(IMAGE) --name $(CLUSTER_NAME)
	kubectl apply --filename cluster/namespace.yaml
	helm upgrade --install $(RELEASE) chart --namespace $(NAMESPACE) \
		--values chart/values-kind.yaml --set-string image.tag=$(GIT_SHA) \
		--wait --timeout $(TIMEOUT)
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

# What CI keeps when a run fails: the events, the state of the pods, their
# logs, and kind's own dump of each node with the kubelet's and every
# container's log. The leading "-" lets one command fail without stopping the
# ones after it.
diagnostics: check-context
	mkdir -p diagnostics
	-kubectl get events --all-namespaces --sort-by=.metadata.creationTimestamp > diagnostics/events.txt
	-kubectl get pods --all-namespaces --output wide > diagnostics/pods.txt
	-kubectl --namespace $(NAMESPACE) describe all > diagnostics/describe.txt
	-kubectl --namespace $(NAMESPACE) logs --selector app.kubernetes.io/instance=$(RELEASE) \
		--all-containers --prefix --tail=-1 > diagnostics/logs.txt
	-kind export logs diagnostics/kind --name $(CLUSTER_NAME)
