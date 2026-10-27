# vxlan-migration developer tasks
.PHONY: help lint test mock mock-netbox syntax molecule ci ee-deps tests-fetch

# Test code and synthetic data live on this branch, never on main.
TEST_BRANCH ?= test
NEED_TESTS = @test -d tests -a -d molecule || { echo "tests/ and molecule/ are on the $(TEST_BRANCH) branch: run make tests-fetch"; exit 1; }

help:
	@echo "make tests-fetch - copy tests/ and molecule/ from origin/$(TEST_BRANCH) (untracked here)"
	@echo "make lint     - yamllint + ansible-lint"
	@echo "make test     - unit tests"
	@echo "make mock     - run discover_vlan.yml end to end against canned CLI output"
	@echo "make mock-netbox - run export_vlan_to_netbox.yml against a mock NetBox (route targets)"
	@echo "make syntax   - ansible-playbook --syntax-check on core playbooks"
	@echo "make molecule - localhost molecule scenarios (vlan_db, service_db)"
	@echo "make ci       - lint + test + syntax + molecule (local CI mirror)"

lint:
	yamllint -c .yamllint .
	ansible-lint -c .ansible-lint

tests-fetch:
	git fetch origin $(TEST_BRANCH)
	rm -rf tests molecule
	git archive FETCH_HEAD tests molecule | tar -x

test:
	$(NEED_TESTS)
	python -m unittest discover -s tests -v

mock:
	$(NEED_TESTS)
	tests/run_discovery_mock.sh

mock-netbox:
	$(NEED_TESTS)
	tests/run_netbox_rt_mock.sh

syntax:
	@set -e; \
	for pb in playbooks/core/*.yml playbooks/check_netbox.yml playbooks/export_vlan_to_netbox.yml; do \
	  echo "==> $$pb"; \
	  ansible-playbook -i inventory/hosts.yml "$$pb" --syntax-check; \
	done

molecule:
	$(NEED_TESTS)
	@command -v molecule >/dev/null || { echo "molecule not found; pip install -r requirements-dev.txt"; exit 1; }
	molecule test -s vlan_db
	molecule test -s service_db

ci: lint test mock mock-netbox syntax molecule

ee-deps:
	pip install -r requirements.txt
	ansible-galaxy collection install -r collections/requirements.yml -p collections/
