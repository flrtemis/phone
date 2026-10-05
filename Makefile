# phone -- convenience targets. Nothing here is required to use the tool;
# ./bin/phone works on its own.

PY ?= python3
PREFIX ?= /opt/phone
BINDIR ?= /usr/local/bin

.PHONY: help test test-python test-shell lint doctor install uninstall clean spool-demo

help:
	@echo "make test         run every suite (no root, no network)"
	@echo "make test-python  python suites only (needs pytest)"
	@echo "make test-shell   sip + firewall + e2e suites"
	@echo "make lint         syntax-check shell scripts and byte-compile python"
	@echo "make doctor       report the state of this host"
	@echo "make install      copy to $(PREFIX) and link $(BINDIR)/phone (needs root)"
	@echo "make spool-demo   deliver a sample message and read it back"

test:
	PHONE_PYTHON=$(PY) ./tests/run.sh

test-python:
	$(PY) -m pytest tests/ -q

test-shell:
	PHONE_PYTHON=$(PY) ./tests/run.sh shell

lint:
	@for f in bin/phone sip/phone.sh sip/gen-certs.sh firewall/lockdown.sh \
	          install/build-pjsip.sh install/install.sh tests/run.sh tests/*.sh; do \
	    [ -f "$$f" ] || continue; \
	    bash -n "$$f" && printf 'ok   %s\n' "$$f" || exit 1; \
	done
	@$(PY) -m compileall -q sms tests && printf 'ok   python sources compile\n'

doctor:
	@PHONE_PYTHON=$(PY) ./bin/phone doctor

install:
	@[ "$$(id -u)" = "0" ] || { echo "make install needs root (sudo make install)" >&2; exit 1; }
	install -d "$(PREFIX)"
	cp -a bin sms sip voice firewall docs systemd install README.md Makefile "$(PREFIX)/"
	install -d "$(BINDIR)"
	ln -sf "$(PREFIX)/bin/phone" "$(BINDIR)/phone"
	@echo "installed to $(PREFIX); run 'phone doctor'"

uninstall:
	rm -f "$(BINDIR)/phone"
	rm -rf "$(PREFIX)"

clean:
	rm -rf $$(find . -name __pycache__ -type d) .pytest_cache
	find . -name '*.pyc' -delete

spool-demo:
	@./bin/phone smsd --spool-dir /tmp/phone-demo-spool --port 8099 >/tmp/phone-demo.log 2>&1 & \
	 pid=$$!; sleep 1; \
	 curl -sS -X POST http://127.0.0.1:8099/sms/incoming \
	      -H 'Content-Type: application/json' \
	      -d '{"from":"+15550109999","text":"demo message"}' >/dev/null; \
	 kill $$pid 2>/dev/null; \
	 ./bin/phone sms --spool-dir /tmp/phone-demo-spool read latest; \
	 rm -rf /tmp/phone-demo-spool /tmp/phone-demo.log
