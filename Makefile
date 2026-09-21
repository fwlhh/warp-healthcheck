.PHONY: lint test install uninstall

lint:
	shellcheck bin/warp-bot.sh scripts/*.sh tests/run.sh tests/cases/*.sh
	shfmt -d -i 2 -ci -sr bin/ scripts/ tests/

test:
	tests/run.sh

install:
	sudo scripts/install.sh

uninstall:
	sudo scripts/uninstall.sh