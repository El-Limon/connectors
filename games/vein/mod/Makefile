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
symbols:          ## dump the wanted symbol table for the installed server
	python3 tools/symprobe.py --binary "$$VEIN_SERVER_BINARY" --write
clean:
	rm -rf build dist tests/build
