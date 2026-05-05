FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG USER_UID=1000
ARG USER_GID=1000

# ── Core system packages ─────────────────────────────────────────────
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    git \
    openssh-client \
    sudo \
    vim \
    python3 \
    python3-pip \
    procps \
    coreutils \
    findutils \
    grep \
    gawk \
    sed \
    less \
    tree \
    file \
    htop \
    jq \
    tmux \
    zip \
    unzip \
    tar \
    gzip \
    bzip2 \
    xz-utils \
    diffutils \
    patch \
    man-db \
    manpages \
    net-tools \
    iputils-ping \
    dnsutils \
    tcpdump \
    libcap2-bin \
    ripgrep \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# ── Create non-root user (match host UID/GID when possible) ──────────
RUN set -eux; \
    if ! getent group "$USER_GID" >/dev/null; then groupadd --gid "$USER_GID" dev; fi; \
    if getent passwd "$USER_UID" >/dev/null; then \
        existing_user="$(getent passwd "$USER_UID" | cut -d: -f1)"; \
        if [ "$existing_user" != "dev" ]; then usermod -l dev "$existing_user"; fi; \
        usermod -d /home/dev -m dev; \
        usermod -g "$USER_GID" dev; \
    else \
        useradd --uid "$USER_UID" --gid "$USER_GID" -m -s /bin/bash dev; \
    fi; \
    echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# ── Allow tcpdump for non-root (monitoring) ──────────────────────────
RUN set -eux;     tcpdump_bin="$(command -v tcpdump || true)";     if [ -n "$tcpdump_bin" ]; then         setcap cap_net_raw,cap_net_admin=eip "$tcpdump_bin" || true;     fi

# ── Install agent ────────────────────────────────────────────────────
ENV NPM_CONFIG_PREFIX="/home/dev/.local"
USER dev
RUN npm install -g --prefix /home/dev/.local @openai/codex
WORKDIR /home/dev


# ── Non-sensitive config defaults (copied into volume on first run) ──
COPY --chown=dev:dev claude-settings/ /home/dev/.claude-defaults/

# ── Placeholder directories ─────────────────────────────────────────
RUN mkdir -p /home/dev/.ssh /home/dev/project /home/dev/.claude /home/dev/.entrypoint.d/hooks

# ── Environment ──────────────────────────────────────────────────────
ENV EDITOR=vim
ENV PATH="/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# ── Entrypoint ───────────────────────────────────────────────────────
COPY --chown=dev:dev entrypoint.sh /home/dev/.entrypoint.d/entrypoint.sh
RUN chmod +x /home/dev/.entrypoint.d/entrypoint.sh
COPY --chown=dev:dev check_monitoring.sh /home/dev/.entrypoint.d/check_monitoring.sh
RUN chmod +x /home/dev/.entrypoint.d/check_monitoring.sh
COPY --chown=dev:dev tmux-attach-banner.sh /home/dev/.entrypoint.d/tmux-attach-banner.sh
RUN chmod +x /home/dev/.entrypoint.d/tmux-attach-banner.sh

ENTRYPOINT ["/home/dev/.entrypoint.d/entrypoint.sh"]

# ── Agent monitoring hooks ────────────────────────────────────────────
COPY --chown=dev:dev hooks/ /home/dev/.entrypoint.d/hooks/
RUN chmod +x /home/dev/.entrypoint.d/hooks/*.sh
COPY --chown=dev:dev .entrypoint.d/ /home/dev/.entrypoint.d/
RUN chmod +x /home/dev/.entrypoint.d/*.sh 2>/dev/null || true
