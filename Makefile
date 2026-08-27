# vxlan-migration developer tasks
.PHONY: help lint test syntax ci ee-deps

help:
	@echo "make lint    - yamllint + ansible-lint"
	@echo "make test    - unit tests"
	@echo "make syntax  - ansible-playbook --syntax-check on core playbooks"
	@echo "make ci      - lint + test + syntax (local CI mirror)"

lint:
	yamllint -c .yamllint .
	ansible-lint -c .ansible-lint

test:
	python -m unittest discover -s tests -v

syntax:
	@set -e; \
	for pb in playbooks/core/*.yml playbooks/check_netbox.yml playbooks/export_vlan_to_netbox.yml; do \
	  echo "==> $$pb"; \
	  ansible-playbook -i inventory/hosts.yml "$$pb" --syntax-check; \
	done

ci: lint test syntax

ee-deps:
	pip install -r requirements.txt
	ansible-galaxy collection install -r collections/requirements.yml -p collections/
