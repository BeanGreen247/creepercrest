CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -fno-plt -fstack-protector-strong -D_FORTIFY_SOURCE=2
LDFLAGS  ?= -Wl,-z,relro,-z,now
LIBS      = -lssl -lcrypto -lzip -lz -pthread
SRC       = $(wildcard src/*.cpp)
OBJ       = $(SRC:src/%.cpp=build/%.o)

creepercrest: $(OBJ)
	$(CXX) $(LDFLAGS) -o $@ $(OBJ) $(LIBS)

build/editor.js.gz: ui/editor.js
	@mkdir -p build
	gzip -9 -n -c $< > $@

build/assets.o: src/assets.cpp ui/index.html build/editor.js.gz
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -c $< -o $@

build/%.o: src/%.cpp $(wildcard src/*.h)
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -c $< -o $@

clean:
	rm -rf build creepercrest
.PHONY: clean
