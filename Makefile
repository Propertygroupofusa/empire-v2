# One command per thing a contributor actually does.
#
# This repo has 283 modules and 356 test modules in a flat root, and before
# this file the only way to run the suite was to hand-write a shell loop -
# which is how the invocation kept drifting (`-m unittest` collects a
# different set and changes the failure count). One command, one meaning.
.PHONY: help test test-new baseline check app lint-syntax

help:
	@echo "make test       - every test module, compared to test_baseline.txt"
	@echo "make test-new   - same, but fails ONLY on a new failure (what CI runs)"
	@echo "make baseline   - rewrite test_baseline.txt from the current state"
	@echo "make check      - compile-check every file (what CI ran before)"
	@echo "make app        - smoke-import the FastAPI app"
	@echo "make T=name test-one - run the modules matching a substring"

test:
	@./run_tests.sh

test-new:
	@./run_tests.sh

test-one:
	@./run_tests.sh $(T)

baseline:
	@./run_tests.sh --update

check:
	@find . -name "*.py" -not -path "./.git/*" -print0 | xargs -0 -n1 python3 -m py_compile
	@echo "every Python file compiles"

app:
	@python3 -c "from main import app; print('FastAPI app loaded OK')"
