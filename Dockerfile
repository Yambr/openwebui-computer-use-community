# AI Computer Use - Dockerfile
# Based on Ubuntu 24.04 Noble Numbat

FROM ubuntu:24.04

LABEL maintainer="OpenWebUI Implementation"
LABEL description="AI Computer Use Environment"
LABEL version="1.0.0"

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_PATH=/home/assistant/.npm-global/lib/node_modules \
    PATH=/home/assistant/.npm-global/bin:/home/assistant/.local/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    GLAB_NO_UPDATE_NOTIFIER=1

# Update and install system packages
RUN apt-get update && apt-get install -y \
    # Build essentials
    build-essential \
    gcc \
    g++ \
    make \
    binutils \
    dpkg-dev \
    # Python
    python3 \
    python3-pip \
    python3-dev \
    python3-venv \
    # Node.js (will install specific version later)
    curl \
    wget \
    ca-certificates \
    gnupg \
    # Git and version control
    git \
    # Compression tools
    zip \
    unzip \
    bzip2 \
    # Text editors
    vim \
    nano \
    # Image processing dependencies
    libmagickwand-dev \
    imagemagick \
    # Graphics libraries
    libcairo2-dev \
    libpango1.0-dev \
    libjpeg-dev \
    libgif-dev \
    librsvg2-dev \
    # OCR dependencies
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-rus \
    # PDF dependencies
    poppler-utils \
    ghostscript \
    qpdf \
    # Document conversion
    pandoc \
    # Video/audio processing
    ffmpeg \
    # Java (for tabula-py and LibreOffice)
    default-jre-headless \
    openjdk-21-jre-headless \
    # LibreOffice (for unoserver)
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    # Fonts
    fontconfig \
    fonts-liberation \
    fonts-liberation2 \
    fonts-dejavu \
    fonts-dejavu-core \
    fonts-dejavu-extra \
    fonts-freefont-ttf \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    # Graphics and rendering
    graphviz \
    # System utilities
    bc \
    file \
    jq \
    dbus \
    # Networking
    apt-transport-https \
    # Clean up
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 22.x via binary distribution (more reliable than nodesource)
RUN curl -fsSL https://nodejs.org/dist/v22.11.0/node-v22.11.0-linux-x64.tar.xz -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz

# Verify versions
RUN python3 --version && \
    node --version && \
    npm --version

# Create python symlink to python3 for compatibility
# Many scripts and tools expect 'python' command to be available
RUN ln -s /usr/bin/python3 /usr/bin/python

# Create a non-root user with sudo access FIRST
RUN useradd -m -s /bin/bash assistant && \
    apt-get update && apt-get install -y sudo && \
    echo "assistant ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Configure npm global directory and sudo to preserve needed ENV variables
RUN mkdir -p /home/assistant/.npm-global && \
    chown -R assistant:assistant /home/assistant/.npm-global && \
    echo 'Defaults env_keep += "NODE_PATH PLAYWRIGHT_BROWSERS_PATH PATH JAVA_HOME NODE_EXTRA_CA_CERTS REQUESTS_CA_BUNDLE SSL_CERT_FILE PYTHONUNBUFFERED PIP_ROOT_USER_ACTION PIP_BREAK_SYSTEM_PACKAGES PYTHONDONTWRITEBYTECODE"' >> /etc/sudoers

# Copy and install Python dependencies (as root first for system-wide availability)
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages --ignore-installed -r /tmp/requirements.txt

# Pre-register Cyrillic and Emoji fonts in reportlab
# Append font registration to reportlab/__init__.py (runs after full initialization)
RUN REPORTLAB_INIT=$(python3 -c "import reportlab; print(reportlab.__file__)") && \
    printf '\n# Auto-register Cyrillic and Emoji fonts\ntry:\n    from reportlab.pdfbase import pdfmetrics\n    from reportlab.pdfbase.ttfonts import TTFont\n    from reportlab.pdfbase.pdfmetrics import registerFontFamily\n    pdfmetrics.registerFont(TTFont("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))\n    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))\n    pdfmetrics.registerFont(TTFont("DejaVuSans-Oblique", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"))\n    pdfmetrics.registerFont(TTFont("DejaVuSans-BoldOblique", "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"))\n    registerFontFamily("DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold", italic="DejaVuSans-Oblique", boldItalic="DejaVuSans-BoldOblique")\n    pdfmetrics.registerFont(TTFont("NotoEmoji", "/usr/share/fonts/truetype/styrene/NotoEmoji-Regular.ttf"))\nexcept Exception:\n    pass\n' >> "$REPORTLAB_INIT"

# Note: pip uses default PyPI index
# For custom index, set PIP_INDEX_URL environment variable

# Copy and install Node.js dependencies globally as assistant user
COPY package.json /tmp/package.json
RUN chown assistant:assistant /tmp/package.json && \
    cd /tmp && \
    sudo -u assistant bash -c "npm config set prefix '/home/assistant/.npm-global' && npm install -g \$(node -pe \"Object.entries(require('./package.json').dependencies).map(([pkg, ver]) => pkg + '@' + ver).join(' ')\")" && \
    npm cache clean --force

# Install Node.js dependencies locally in /home/assistant for ES Modules support
# Global installation above provides CLI tools and CommonJS require() support
# Local installation provides ES modules import support (resolves from node_modules)
# npm reuses cache from global install, so this is fast and space-efficient
COPY package.json /home/assistant/package.json
RUN chown assistant:assistant /home/assistant/package.json && \
    cd /home/assistant && \
    sudo -u assistant bash -c "npm install --prefer-offline" && \
    rm -f /home/assistant/package.json  # Remove package.json, keep only node_modules

# Install Playwright browsers (only once, shared by both Python and Node.js)
RUN python3 -m playwright install --with-deps chromium && \
    chmod -R 755 /opt/pw-browsers

# Copy and install custom fonts
COPY fonts/ /usr/share/fonts/truetype/styrene/
RUN fc-cache -f -v

# Create directory structure with proper ownership
RUN mkdir -p /mnt/user-data/uploads \
             /mnt/user-data/outputs \
             /mnt/skills \
             /mnt/transcripts && \
    chown -R root:root /mnt/user-data/uploads /mnt/skills && \
    chown -R assistant:assistant /mnt/user-data/outputs /mnt/transcripts && \
    chmod 755 /mnt/user-data/uploads /mnt/skills && \
    chmod 755 /mnt/user-data/outputs /mnt/transcripts

# Copy skills into image (available in all containers)
COPY --chown=root:root ./skills /mnt/skills/

# Install html2pptx from local .tgz file (required for PPTX skill)
# This package has dependencies that require network access during install,
# so we install it from pre-built .tgz to work in offline environments
RUN chown assistant:assistant /mnt/skills/public/pptx/html2pptx.tgz && \
    sudo -u assistant bash -c "cd /tmp && npm install -g /mnt/skills/public/pptx/html2pptx.tgz" && \
    npm cache clean --force

# Install glab CLI for GitLab operations
RUN curl -fsSL https://gitlab.com/gitlab-org/cli/-/releases/v1.52.0/downloads/glab_1.52.0_linux_amd64.tar.gz \
    | tar -xzf - -C /tmp && \
    mv /tmp/bin/glab /usr/local/bin/glab && \
    chmod +x /usr/local/bin/glab && \
    rm -rf /tmp/bin /tmp/LICENSE && \
    sudo -u assistant glab config set check_update false --global

# Install Claude Code CLI
RUN sudo -u assistant bash -c "npm install -g @anthropic-ai/claude-code" && \
    npm cache clean --force

# Create entrypoint script that configures git/glab and Claude Code
# This runs on container start and sets up dynamic configuration based on env vars
RUN printf '#!/bin/bash\n\
# Configure GitLab\n\
if [ -n "$GITLAB_TOKEN" ]; then\n\
    GITLAB_HOST="${GITLAB_HOST:-gitlab.com}"\n\
    git config --global url."https://oauth2:${GITLAB_TOKEN}@${GITLAB_HOST}/".insteadOf "https://${GITLAB_HOST}/"\n\
    echo "Git configured for $GITLAB_HOST with token auth"\n\
else\n\
    echo "No GITLAB_TOKEN - git/glab will work without auth (public repos only)"\n\
fi\n\
\n\
# Configure Claude Code\n\
if [ -n "$ANTHROPIC_API_KEY" ]; then\n\
    export ANTHROPIC_API_KEY\n\
    export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"\n\
    if [ -n "$ANTHROPIC_CUSTOM_HEADERS" ]; then\n\
        export ANTHROPIC_CUSTOM_HEADERS\n\
    fi\n\
    echo "Claude Code configured with base URL: $ANTHROPIC_BASE_URL"\n\
else\n\
    echo "No ANTHROPIC_API_KEY - Claude Code will not work"\n\
fi\n\
\n\
exec "$@"\n' > /home/assistant/.entrypoint.sh && \
    chmod +x /home/assistant/.entrypoint.sh && \
    chown assistant:assistant /home/assistant/.entrypoint.sh

# Configure git defaults (user info)
RUN printf '[user]\n\
    name = AI Assistant\n\
    email = ai-assistant@example.com\n' > /home/assistant/.gitconfig && \
    chown assistant:assistant /home/assistant/.gitconfig

# Set working directory
WORKDIR /home/assistant

# Verify installations
RUN python3 -c "import docx, pptx, openpyxl; print('Python packages OK')" && \
    node -e "console.log('Node.js OK')" && \
    npm list -g --depth=0 && \
    sudo -u assistant bash -c "export PATH=/home/assistant/.npm-global/bin:\$PATH && claude --version" && echo "Claude Code OK"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "print('healthy')" || exit 1

# Default command (can be overridden)
CMD ["/bin/bash"]
