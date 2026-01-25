# Contributing to Open WebUI Computer Use

Thank you for your interest in contributing! This document provides guidelines for contributing to the project.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/your-username/openwebui-computer-use.git`
3. Create a feature branch: `git checkout -b feature/your-feature`

## Development Setup

```bash
# Copy environment file
cp .env.example .env

# Build containers
docker-compose build

# Start file server for development
docker-compose up file-server
```

## Code Style

### Python
- Follow PEP 8 guidelines
- Use type hints where practical
- Keep functions focused and well-documented

### Documentation
- Update relevant docs when changing features
- Use clear, concise language
- Include examples where helpful

## Pull Request Process

1. Ensure your code builds and runs without errors
2. Update documentation if you change functionality
3. Write a clear PR description explaining your changes
4. Reference any related issues

## Types of Contributions

### Bug Reports
- Use the GitHub issue tracker
- Include steps to reproduce
- Include relevant logs and error messages

### Feature Requests
- Describe the use case
- Explain the expected behavior
- Consider backwards compatibility

### Code Contributions
- Bug fixes
- New features
- Documentation improvements
- Performance optimizations

## Skills Development

If you're creating a new skill:

1. Create a directory under `skills/public/` or `skills/examples/`
2. Include a `SKILL.md` with:
   - Skill name and description
   - Usage instructions
   - Example commands
3. Include any necessary scripts in a `scripts/` subdirectory

## Testing

Before submitting:

```bash
# Test Docker build
docker-compose build

# Test file server
docker-compose up file-server
curl http://localhost:8081/health

# Test container creation (requires running file-server)
curl -X POST http://localhost:8081/mcp \
  -H "Content-Type: application/json" \
  -H "X-Chat-Id: test-123" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Questions?

Open an issue for any questions or concerns.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
