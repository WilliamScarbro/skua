FROM skua-base-codex

# ── Project packages ────────────────────────────────────────────────
USER root
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    latexmk \
    texlive-base \
    texlive-fonts-recommended \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-latex-recommended \
    texlive-publishers \
    texlive-science \
    && rm -rf /var/lib/apt/lists/*

USER dev
WORKDIR /home/dev

