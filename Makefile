.DEFAULT_GOAL := help
sources = sdk tests app

.uv:
	@uv -V || echo 'Please install uv: https://docs.astral.sh/uv/getting-started/installation/'

.pre-commit: .uv
	@uv run pre-commit -V || uv pip install pre-commit

install: .uv
	uv sync --frozen --all-groups --all-extras
	uv pip install pre-commit commitizen
	uv run pre-commit install --install-hooks

format: .uv
	uv run ruff check --fix $(sources)
	uv run ruff format $(sources)

lint: .uv
	uv run ruff check $(sources)
	uv run ruff format --check $(sources)

docstrings: .uv
	uv run pydoclint --config pyproject.toml

codespell: .pre-commit
	pre-commit run codespell --all-files

typecheck: .pre-commit
	pre-commit run typecheck --all-files

test: .uv
	uv run coverage run -m pytest --durations=10

testcov: test
	@echo "building coverage html"
	@uv run coverage html
	@echo "building coverage lcov"
	@uv run coverage lcov

ci: lint docstrings typecheck codespell testcov

clean:
	rm -rf $(find . -name __pycache__)
	rm -f $(find . -type f -name '*.py[co]')
	rm -f $(find . -type f -name '*~')
	rm -f $(find . -type f -name '.*~')
	rm -rf .cache .pytest_cache .ruff_cache htmlcov *.egg-info
	rm -f .coverage .coverage.* coverage.xml
	rm -rf build dist site docs/_build fastapi/test.db

rebuild-lockfile: .uv
	uv lock --upgrade

api-ref:
	uv run pydoc-markdown

docs-build: api-ref
	npm install
	npm run docs:build

docs-preview: api-ref
	npm install
	npm run docs:dev

template-path ?= template.yaml
build-path ?= .aws-sam
local-sam-build:
	sam build -t $(template-path) --build-dir $(build-path) --parallel --use-container --cached  --beta-features --parameter-overrides AtomSDKToken=$(ATOM_SDK_TOKEN)

config-path ?= samconfig.toml
local-sam-deploy:
	sam deploy --config-file $(config-path) --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND

init-sam-deploy:
	sam deploy --guided --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND


sam-publish:
	sam package --output-template-file .aws-sam/packaged.yaml --s3-bucket ada-mode-atom
	sam publish --template .aws-sam/packaged.yaml --region us-east-1

help:
	@grep -E \
		'^.PHONY: .*?## .*$$' $(MAKEFILE_LIST) | \
		sort | \
		awk 'BEGIN {FS = ".PHONY: |## "}; {printf "\033[36m%-19s\033[0m %s\n", $$2, $$3}'
