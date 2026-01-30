# Contributing to AWS-Fast-Channels

Thank you for considering contributing to AWS-Fast-Channels! This document outlines the process for contributing to this project.

## Code of Conduct

Please be respectful and constructive in all interactions. We're all here to build something useful together.

## How to Contribute

### Reporting Bugs

1. Check existing issues to avoid duplicates
2. Create a new issue with:
   - Clear, descriptive title
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (AWS region, Python version, SAM CLI version)
   - Relevant CloudWatch logs (sanitized of sensitive data)

### Suggesting Features

1. Open an issue with the "enhancement" label
2. Describe the use case and expected behavior
3. Consider backwards compatibility implications

### Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes following the style guide
4. Run linting: `ruff check source/`
5. Test locally with SAM CLI
6. Commit with clear messages
7. Push and open a PR against `main`

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/AWS-Fast-Channels.git
cd AWS-Fast-Channels

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dev dependencies
pip install ruff mypy boto3-stubs

# Validate templates
cd deployment
sam validate --template-file fast-on-aws.deployment --lint
```

## Style Guide

### Python

- Python 3.12+ features encouraged
- Type hints required for function signatures
- Docstrings for modules and public functions
- Follow PEP 8 (enforced by Ruff)

### CloudFormation/SAM

- Use `!Ref` and `!Sub` over `Fn::Ref` and `Fn::Sub`
- Descriptive resource logical IDs
- Include descriptions for parameters and outputs
- Follow least-privilege for IAM policies

### Commits

- Use present tense: "Add feature" not "Added feature"
- Reference issues: "Fix #123: Handle duplicate assets"
- Keep commits focused and atomic

## Testing

### Local Testing

```bash
# Build
cd deployment
sam build --template-file fast-on-aws.deployment

# Local invoke (requires Docker)
sam local invoke MediaConvertFunction -e events/s3-event.json
```

### Integration Testing

Deploy to a test AWS account and verify:
1. File upload triggers MediaConvert job
2. MediaPackage asset created successfully
3. MediaTailor VOD source populated
4. Playback URLs work with ad insertion

## Questions?

Open an issue or start a discussion. We're happy to help!
