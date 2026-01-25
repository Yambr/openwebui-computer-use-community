#!/bin/bash

# AI Computer Use - Enhanced Setup Script
# Version: 2.0.0
# This script prepares the environment for running the container

set -e  # Exit on error
set -u  # Exit on undefined variable

# ==============================================================================
# Configuration
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ==============================================================================
# Helper Functions
# ==============================================================================

log_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

check_command() {
    if command -v "$1" &> /dev/null; then
        return 0
    else
        return 1
    fi
}

# ==============================================================================
# Banner
# ==============================================================================

echo ""
echo "╔════════════════════════════════════════════════════════╗"
echo "║     AI Computer Use - Setup Script v2.0.0             ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""

# ==============================================================================
# Prerequisites Check
# ==============================================================================

log_info "Checking prerequisites..."

# Check Docker
if ! check_command docker; then
    log_error "Docker is not installed"
    echo "  Please install Docker first: https://docs.docker.com/get-docker/"
    exit 1
fi
log_success "Docker is installed ($(docker --version))"

# Check Docker Compose
if ! check_command docker-compose && ! docker compose version &> /dev/null; then
    log_error "Docker Compose is not installed"
    echo "  Please install Docker Compose: https://docs.docker.com/compose/install/"
    exit 1
fi

# Determine compose command
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
else
    COMPOSE_CMD="docker-compose"
fi
log_success "Docker Compose is installed"

# Check if Docker daemon is running
if ! docker info &> /dev/null; then
    log_error "Docker daemon is not running"
    echo "  Please start Docker and try again"
    exit 1
fi
log_success "Docker daemon is running"

echo ""

# ==============================================================================
# Directory Structure
# ==============================================================================

log_info "Creating directory structure..."

mkdir -p data/uploads data/outputs
log_success "Created data directories"

# Create .gitkeep files if they don't exist
touch data/uploads/.gitkeep data/outputs/.gitkeep

echo ""

# ==============================================================================
# Skills System Check
# ==============================================================================

log_info "Checking skills system..."

if [ ! -d "skills" ]; then
    log_warning "Skills directory not found"
    echo "  The skills system provides enhanced capabilities for AI assistants."
    echo "  You can continue without it, but some features will be unavailable."
    echo ""
    read -p "Continue without skills? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_error "Setup cancelled"
        exit 1
    fi
else
    # Check if skills directory is not empty
    if [ "$(ls -A skills 2>/dev/null)" ]; then
        log_success "Skills system is present"
    else
        log_warning "Skills directory is empty"
    fi
fi

echo ""

# ==============================================================================
# Environment Variables
# ==============================================================================

log_info "Checking environment configuration..."

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        log_success "Created .env from .env.example"
        log_warning "Please review and customize .env as needed"
    else
        log_warning ".env.example not found - skipping .env creation"
    fi
else
    log_info ".env file already exists"
fi

echo ""

# ==============================================================================
# Configuration Files Check
# ==============================================================================

log_info "Verifying configuration files..."

required_files=("Dockerfile" "docker-compose.yml" "requirements.txt" "package.json" "apt-packages.txt")
missing_files=()

for file in "${required_files[@]}"; do
    if [ ! -f "$file" ]; then
        missing_files+=("$file")
    fi
done

if [ ${#missing_files[@]} -gt 0 ]; then
    log_error "Missing required files:"
    for file in "${missing_files[@]}"; do
        echo "  - $file"
    done
    exit 1
fi

log_success "All configuration files present"
echo ""

# ==============================================================================
# Docker Image Build
# ==============================================================================

log_info "Building Docker image..."
echo "  This may take 10-15 minutes on first build..."
echo ""

if $COMPOSE_CMD build --progress=plain; then
    log_success "Docker image built successfully"
else
    log_error "Docker build failed"
    exit 1
fi

echo ""

# ==============================================================================
# Installation Verification
# ==============================================================================

log_info "Verifying installation..."
echo ""

echo "  Checking Python..."
$COMPOSE_CMD run --rm ai-computer-use python3 --version

echo "  Checking Node.js..."
$COMPOSE_CMD run --rm ai-computer-use node --version

echo "  Checking npm..."
$COMPOSE_CMD run --rm ai-computer-use npm --version

echo ""
log_success "All components verified"
echo ""

# ==============================================================================
# Completion
# ==============================================================================

echo ""
echo "╔════════════════════════════════════════════════════════╗"
echo "║                  Setup Complete!                       ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""
log_info "Quick Start Commands:"
echo ""
echo "  Start container:"
echo "    $COMPOSE_CMD up -d"
echo ""
echo "  Access container shell:"
echo "    $COMPOSE_CMD exec ai-computer-use /bin/bash"
echo ""
echo "  View logs:"
echo "    $COMPOSE_CMD logs -f"
echo ""
echo "  Stop container:"
echo "    $COMPOSE_CMD down"
echo ""
echo "  Restart container:"
echo "    $COMPOSE_CMD restart"
echo ""
log_info "Directory Structure:"
echo ""
echo "  ./data/uploads/  - Place input files here"
echo "  ./data/outputs/  - Generated files appear here"
echo "  ./skills/        - Skills system (if available)"
echo ""
log_warning "Next Steps:"
echo ""
echo "  1. Review and customize .env file if needed"
echo "  2. Start the container with: $COMPOSE_CMD up -d"
echo "  3. Check container health: $COMPOSE_CMD ps"
echo ""
