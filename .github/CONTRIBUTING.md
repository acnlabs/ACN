# Contributing to ACN

Thank you for your interest in contributing to ACN (Agent Collaboration Network)! This document provides guidelines for contributing to the project.

## 📋 Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Documentation](#documentation)

---

## Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct. Please be respectful and constructive in all interactions.

---

## Getting Started

### Prerequisites

- Python 3.11 or higher
- Node.js 20+ (for TypeScript SDK)
- Redis (for development)
- Docker (optional, for containerized development)

### Finding Issues

- Look for issues labeled `good first issue` for beginner-friendly tasks
- Check `help wanted` for issues where maintainers need assistance
- Feel free to create a new issue if you find a bug or have a feature idea

---

## Development Setup

### 1. Clone the Repository

```bash
git clone https://github.com/acnlabs/ACN.git
cd ACN
```

### 2. Install Dependencies

```bash
# Install uv (recommended Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv sync --extra dev

# For TypeScript SDK development
cd clients/typescript
npm install
```

### 3. Start Development Services

```bash
# Start Redis
docker-compose up -d redis

# Run ACN server in development mode
uv run uvicorn acn.api:app --reload --host 0.0.0.0 --port 8000
```

### 4. Verify Setup

```bash
# Run tests
uv run pytest -v

# Check code quality
uv run ruff check .
uv run basedpyright
```

---

## Making Changes

### Branch Naming

Use descriptive branch names:

- `feat/add-payment-retry` - New features
- `fix/websocket-reconnect` - Bug fixes
- `docs/update-api-docs` - Documentation
- `refactor/simplify-registry` - Code refactoring
- `test/add-subnet-tests` - Test additions

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description

[optional body]

[optional footer]
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Code style (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding tests
- `chore`: Maintenance tasks
- `ci`: CI/CD changes

**Examples**:
```
feat(payments): add webhook retry mechanism

fix(registry): handle concurrent agent registration

docs(api): update payment endpoint examples
```

---

## Pull Request Process

### Before Submitting

1. **Update from main**: Ensure your branch is up to date
   ```bash
   git fetch origin
   git rebase origin/main
   ```

2. **Run all checks**:
   ```bash
   uv run ruff check .
   uv run basedpyright
   uv run pytest -v
   ```

3. **Update documentation** if needed

### Submitting

1. Push your branch to your fork
2. Create a Pull Request against `main`
3. Fill out the PR template completely
4. Wait for CI checks to pass
5. Request review from maintainers

### Review Process

- Maintainers will review within 2-3 business days
- Address feedback promptly
- Once approved, a maintainer will merge

---

## Coding Standards

### Python

- **Style**: Follow PEP 8, enforced by Ruff
- **Type Hints**: All public functions must have type hints
- **Docstrings**: Google style docstrings for public APIs

```python
async def register_agent(
    agent_id: str,
    name: str,
    endpoint: str,
    skills: list[str] | None = None,
) -> AgentInfo:
    """Register a new agent with the network.

    Args:
        agent_id: Unique identifier for the agent.
        name: Human-readable name.
        endpoint: HTTP endpoint where agent can be reached.
        skills: Optional list of agent capabilities.

    Returns:
        AgentInfo: The registered agent's information.

    Raises:
        ValueError: If agent_id is invalid.
        AgentExistsError: If agent already registered.
    """
```

### TypeScript

- **Style**: ESLint + Prettier
- **Types**: Strict TypeScript, no `any` unless necessary
- **Exports**: Named exports preferred

```typescript
/**
 * Search for agents matching criteria
 * @param options - Search options
 * @returns List of matching agents
 */
export async function searchAgents(
  options: AgentSearchOptions
): Promise<AgentInfo[]> {
  // Implementation
}
```

---

## Testing

### Running Tests

```bash
# All tests
uv run pytest

# Specific test file
uv run pytest tests/test_registry.py

# With coverage
uv run pytest --cov=acn --cov-report=html

# TypeScript SDK tests
cd clients/typescript
npm test
```

### Writing Tests

- Place tests in `tests/` directory
- Mirror the source structure: `acn/registry.py` → `tests/test_registry.py`
- Use descriptive test names: `test_register_agent_with_payment_capability`
- Include both positive and negative test cases

```python
import pytest
from acn.registry import AgentRegistry


class TestAgentRegistry:
    @pytest.fixture
    async def registry(self, mock_redis):
        return AgentRegistry(redis=mock_redis)

    async def test_register_agent_success(self, registry):
        """Test successful agent registration."""
        result = await registry.register_agent(
            agent_id="test-agent",
            name="Test Agent",
            endpoint="http://localhost:8001",
        )
        assert result.agent_id == "test-agent"
        assert result.status == "online"

    async def test_register_agent_duplicate_fails(self, registry):
        """Test that duplicate registration raises error."""
        await registry.register_agent(
            agent_id="test-agent",
            name="Test Agent",
            endpoint="http://localhost:8001",
        )
        with pytest.raises(AgentExistsError):
            await registry.register_agent(
                agent_id="test-agent",
                name="Another Agent",
                endpoint="http://localhost:8002",
            )
```

---

## Documentation

### When to Update Docs

- Adding new API endpoints
- Changing existing behavior
- Adding new features
- Fixing unclear documentation

### Documentation Files

| File | Purpose |
|------|---------|
| `README.md` | Project overview, quick start |
| `docs/api.md` | API reference |
| `docs/architecture.md` | System design |
| `clients/*/README.md` | SDK documentation |

### Writing Style

- Use clear, concise language
- Include code examples
- Keep paragraphs short
- Use tables for structured data

---

## Questions?

- Open a [Discussion](https://github.com/acnlabs/ACN/discussions)
- Check existing [Issues](https://github.com/acnlabs/ACN/issues)
- Join our community chat (coming soon)

Thank you for contributing to ACN! 🚀
































