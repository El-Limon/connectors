.PHONY: all build native test clean shell
all: build
build:            ## build the .so in the debian:bookworm container
	./build.sh
native:           ## build on the host toolchain
	./build.sh --native
test:             ## run the unit tests in the container
	./tests/run.sh
test-native:      ## run the unit tests on the host toolchain (already inside the container)
	./tests/run.sh --native
vtables:          ## list the exported _ZTV* vtables matching NEEDLE in the installed server
	python3 tools/vtprobe.py "$$DUNE_SERVER_BINARY" find "$$NEEDLE"
slots:            ## dump the slots of one vtable (ZTV=_ZTV7UObject)
	python3 tools/vtprobe.py "$$DUNE_SERVER_BINARY" slots "$$ZTV"
clean:
	rm -rf build dist tests/build
