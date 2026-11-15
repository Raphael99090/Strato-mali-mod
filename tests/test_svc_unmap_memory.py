#!/usr/bin/env python3
"""Compile and exercise the production SvcUnmapMemory implementation with split chunk maps."""
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
base = root / "app/src/main/cpp/skyline/kernel"
header = (base / "memory.h").read_text()
cpp = (base / "memory.cpp").read_text()

types = header[header.index("    namespace memory {"):header.index(
    "        /**\n         * @brief A memory region representing"
)]
functions = cpp[cpp.index("    void MemoryManager::MapInternal("):cpp.index(
    "    void MemoryManager::ForeachChunkInRange("
)]
functions += cpp[cpp.index("    bool MemoryManager::CollectSvcMemoryRange("):cpp.index(
    "    bool MemoryManager::SvcSetMemoryAttribute("
)]
functions += cpp[cpp.index("    void MemoryManager::SvcUnmapMemory("):cpp.index(
    "    void MemoryManager::AddRef("
)]

source = r"""
#include <algorithm>
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <map>
#include <mutex>
#include <shared_mutex>
#include <span>
#include <string>
#include <vector>
#include <sys/mman.h>
using u8 = uint8_t;
using u32 = uint32_t;
using u64 = uint64_t;
template<class T> using span = std::span<T>;
#define LOGW(...) ((void)0)
namespace skyline {
TYPES
class MemoryManager {
public:
    std::map<u8 *, ChunkDescriptor> chunks;
    std::shared_mutex mutex;
    span<u8> GetHostSpan(span<u8> s) { return s; }
    void MapInternal(const std::pair<u8 *, ChunkDescriptor> &);
    bool CollectSvcMemoryRange(span<u8>, u32, u8,
        std::vector<std::pair<u8 *, ChunkDescriptor>> &);
    void SvcUnmapMemory(span<u8>, span<u8>);
};
FUNCTIONS
}
}
int main() {
    using namespace skyline::kernel;
    alignas(4096) static u8 bytes[0x10000];
    MemoryManager manager;

    auto makeDescriptor = [](size_t size, memory::MemoryState state,
                             memory::Permission permission, bool borrowed) {
        ChunkDescriptor d{};
        d.isSrcMergeDisallowed = true;
        d.permission = permission;
        d.state = state;
        d.attributes.isBorrowed = borrowed;
        d.size = size;
        return d;
    };

    auto reset = [&](bool validSource = true, bool validDestination = true) {
        manager.chunks.clear();
        ChunkDescriptor unmapped{};
        unmapped.state = memory::states::Unmapped;
        unmapped.size = 0x1000;
        manager.chunks.emplace(bytes, unmapped);

        auto sourceFirst = makeDescriptor(0x1000, memory::states::Stack,
            memory::Permission{false, false, false}, true);
        auto sourceSecond = sourceFirst;
        if (!validSource) sourceSecond.attributes.isBorrowed = false;
        manager.chunks.emplace(bytes + 0x1000, sourceFirst);
        manager.chunks.emplace(bytes + 0x2000, sourceSecond);

        unmapped.size = 0x5000;
        manager.chunks.emplace(bytes + 0x3000, unmapped);

        auto destinationFirst = makeDescriptor(0x1000, memory::states::Stack,
            memory::Permission{true, true, false}, false);
        auto destinationSecond = destinationFirst;
        if (!validDestination) destinationSecond.permission =
            memory::Permission{true, false, false};
        manager.chunks.emplace(bytes + 0x8000, destinationFirst);
        manager.chunks.emplace(bytes + 0x9000, destinationSecond);

        unmapped.size = 0x6000;
        manager.chunks.emplace(bytes + 0xA000, unmapped);
        ChunkDescriptor sentinel{};
        sentinel.state = memory::states::Reserved;
        manager.chunks.emplace(bytes + sizeof(bytes), sentinel);
    };

    auto sourceFirst = [&]() -> ChunkDescriptor & {
        return manager.chunks.at(bytes + 0x1000);
    };
    auto sourceSecond = [&]() -> ChunkDescriptor & {
        return manager.chunks.at(bytes + 0x2000);
    };

    // Complete valid unmap across two destination and two source descriptors.
    reset();
    std::memset(bytes + 0x1000, 0x11, 0x2000);
    std::memset(bytes + 0x8000, 0x7A, 0x2000);
    manager.SvcUnmapMemory({bytes + 0x1000, 0x2000},
                           {bytes + 0x8000, 0x2000});
    assert(std::memcmp(bytes + 0x1000, bytes + 0x8000, 0x2000) == 0);
    for (auto address : {bytes + 0x1000, bytes + 0x2000}) {
        const auto &d = manager.chunks.at(address);
        assert((d.permission == memory::Permission{true, true, false}));
        assert(!d.attributes.isBorrowed);
        assert(d.state == memory::states::Stack);
    }

    // A bad later source chunk must reject the entire operation before copying
    // or restoring the earlier valid chunk.
    reset(false, true);
    std::memset(bytes + 0x1000, 0x11, 0x2000);
    std::memset(bytes + 0x8000, 0x7A, 0x2000);
    manager.SvcUnmapMemory({bytes + 0x1000, 0x2000},
                           {bytes + 0x8000, 0x2000});
    assert(memory[0x1000] == 0x11);
    assert((sourceFirst().permission == memory::Permission{false, false, false}));
    assert(sourceFirst().attributes.isBorrowed);
    assert(!sourceSecond().attributes.isBorrowed);

    // A bad later destination descriptor must also reject atomically.
    reset(true, false);
    std::memset(bytes + 0x1000, 0x11, 0x2000);
    std::memset(bytes + 0x8000, 0x7A, 0x2000);
    manager.SvcUnmapMemory({bytes + 0x1000, 0x2000},
                           {bytes + 0x8000, 0x2000});
    assert(memory[0x1000] == 0x11);
    assert(sourceFirst().attributes.isBorrowed);

    // Same/overlapping ranges are rejected without touching the descriptors.
    reset();
    manager.SvcUnmapMemory({bytes + 0x1000, 0x1000},
                           {bytes + 0x1000, 0x1000});
    assert(sourceFirst().attributes.isBorrowed);
}
""".replace("TYPES", types).replace("FUNCTIONS", functions)

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    cpp_file = tmp / "svc_unmap_memory_test.cpp"
    binary = tmp / "svc_unmap_memory_test"
    cpp_file.write_text(source)
    subprocess.run([
        os.environ.get("CXX", "c++"), "-std=c++20", "-Wall", "-Wextra",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        str(cpp_file), "-o", str(binary)
    ], check=True)
    subprocess.run([str(binary)], check=True)

print("PASS: SvcUnmapMemory split ranges, copy-back, permission restoration, and invalid-range rejection")
