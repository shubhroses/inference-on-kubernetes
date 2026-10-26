# The Python image both stages start from: Docker Hub's official one, pulled
# from Google's mirror of Docker Hub (versions.env says why). The tag is for
# the reader; the digest is what gets pulled. versions.env records the same
# reference, and CI fails if the two differ. Dependabot proposes changes to
# this line and does not read versions.env, so PYTHON_IMAGE there has to be
# given the same value by hand in its pull request.
FROM mirror.gcr.io/library/python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS base


# Build stage: install the locked dependencies into a virtualenv.
FROM base AS build

COPY requirements.txt /tmp/requirements.txt

# The virtualenv is created without a pip of its own, and the base image's pip
# installs into it, so that it holds the service's dependencies and nothing
# else. --require-hashes refuses any package that is not in the lock file or
# whose download does not match the hash recorded there. --only-binary keeps
# to prebuilt wheels, so installing never runs a package's own build script.
RUN python -m venv --without-pip /opt/venv \
    && pip --python /opt/venv/bin/python install --no-cache-dir --require-hashes \
        --only-binary :all: --requirement /tmp/requirements.txt


# Runtime stage: the interpreter, the virtualenv and the application.
FROM base AS runtime

# pip was needed only to build, so the runtime goes without it: the installed
# copy, and the wheel the standard library keeps for creating virtualenvs. pip
# bundles libraries for its own use (urllib3 among them), and an image should
# not carry code that has to be kept patched and is never run.
RUN pip uninstall --yes --root-user-action ignore pip \
    && find /usr/local/lib -name 'pip-*.whl' -delete

COPY --from=build /opt/venv /opt/venv

# `python -m app` finds the package in the working directory.
WORKDIR /srv
COPY app/ app/

# Putting the virtualenv first on the path is all that "activating" it does.
# The second variable tells Python not to try to cache compiled modules next
# to their source, because the root filesystem is meant to be read-only. The
# dependencies need no compiling: pip did that when it installed them.
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1

# The commit the image was built from, reported by the build-info metric.
# Declared this late so that a new commit reuses every layer above.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}
LABEL org.opencontainers.image.revision=${GIT_SHA}

# A numeric user: Kubernetes can only check runAsNonRoot against a number. It
# has no entry in /etc/passwd and owns nothing, so the process can read the
# application and the virtualenv but not change them.
USER 10001:10001

EXPOSE 8000

# Exec form: Python is process 1 and receives SIGTERM itself, which starts the drain.
CMD ["python", "-m", "app"]
