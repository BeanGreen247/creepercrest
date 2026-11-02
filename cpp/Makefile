# Build options (y/n; anything else, or leaving them out, means the default):
#   make O2=y               use -O2 instead of the default -O3
#   make DISABLE_2FA=y      password-only login, no authenticator code (default: n)
#   make PANEL_POWER=y       always show the panel Restart / Shut down buttons (default: only on LAN/local setups)
#   make DISABLE_BACKUPS=y  compile the backup feature out (default: n)
#   make DEBUG=y            debug build: -Og -g, request logging, no -DNDEBUG (default: n)
#   make cmake              run cmake, then make, with the same options
#   make release            also copy the binary into build/release-<architecture>/
# Example: make O2=y DISABLE_BACKUPS=y DEBUG=n
yes = $(filter y yes 1 true on,$(call lc,$(strip $(1))))
lc  = $(shell printf '%s' '$(1)' | tr 'A-Z' 'a-z')

OPT  := -O3
DEFS :=
ifneq ($(call yes,$(O2)),)
OPT := -O2
endif
ifneq ($(call yes,$(DISABLE_2FA)),)
DEFS += -DCC_DISABLE_2FA
endif
ifneq ($(call yes,$(DISABLE_BACKUPS)),)
DEFS += -DCC_DISABLE_BACKUPS
endif
ifneq ($(call yes,$(PANEL_POWER)),)
DEFS += -DCC_PANEL_POWER
endif
ifneq ($(call yes,$(DEBUG)),)
OPT  := -Og -g
DEFS += -DCC_DEBUG
endif

CXX      ?= g++
CXXFLAGS ?= -std=c++17 $(OPT) $(DEFS) -Wall -Wextra -fno-plt -fstack-protector-strong -D_FORTIFY_SOURCE=2
LDFLAGS  ?= -Wl,-z,relro,-z,now
LIBS      = -lssl -lcrypto -lzip -lz -pthread
SRC       = $(wildcard src/*.cpp)
OBJ       = $(SRC:src/%.cpp=build/%.o)

# Rebuild everything when the flags change (a stamp records the last flags used).
STAMP = build/.flags
$(shell mkdir -p build; echo '$(CXX) $(CXXFLAGS)' | cmp -s - $(STAMP) || echo '$(CXX) $(CXXFLAGS)' > $(STAMP))

creepercrest: $(OBJ)
	$(CXX) $(LDFLAGS) -o $@ $(OBJ) $(LIBS)

build/editor.js.gz: ui/editor.js
	@mkdir -p build
	gzip -9 -n -c $< > $@

build/assets.o: src/assets.cpp ui/index.html build/editor.js.gz $(STAMP)
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -c $< -o $@

build/%.o: src/%.cpp $(wildcard src/*.h) $(STAMP)
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -c $< -o $@

# make cmake  ->  run cmake, then make, with the same options (for toolchains or architectures that want CMake):
#   make cmake O2=y DISABLE_BACKUPS=y
# The binary still ends up at cpp/creepercrest.
CMAKE_OPTS = $(foreach o,O2 DISABLE_2FA DISABLE_BACKUPS PANEL_POWER DEBUG,-D$(o)=$(if $(call yes,$($(o))),ON,OFF))

cmake:
	cmake -G "Unix Makefiles" -S . -B build/cmake $(CMAKE_OPTS)
	$(MAKE) -C build/cmake

# make release  ->  build/release-<architecture>/ with the binary and a checksum.
# The architecture is `uname -m` of the machine building it; name it yourself for a cross build:
#   make release CXX=aarch64-linux-gnu-g++ ARCH=aarch64
ARCH   ?= $(shell uname -m)
RELDIR  = build/release-$(ARCH)

release: creepercrest
	@mkdir -p $(RELDIR)
	cp creepercrest $(RELDIR)/creepercrest
	cp creepercrest.service $(RELDIR)/creepercrest.service
	cd $(RELDIR) && sha256sum creepercrest > SHA256SUMS
	@echo "Release files are in cpp/$(RELDIR)/"

clean:
	rm -rf build creepercrest
.PHONY: clean release cmake
