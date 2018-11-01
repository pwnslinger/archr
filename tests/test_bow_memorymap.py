import archr
import os

def setup_module():
    os.system("cd %s/dockers; ./build_all.sh" % os.path.dirname(__file__))

def test_cat_ldd():
    with archr.targets.DockerImageTarget('archr-test:cat').build() as t:
        b = archr.bows.MemoryMapBow(t)
        s = b.fire()
        assert s == {
            'linux-vdso.so.1': 0x7ffff7ffa000,
            '/lib/x86_64-linux-gnu/libc.so.6': 0x7ffff77c4000,
            '/lib64/ld-linux-x86-64.so.2': 0x7ffff7dd5000,
            '[stack-end]': 0x7ffffffff000,
            '[heap]': 0x55555575d000,
            '[vvar]': 0x7ffff7ff7000,
            '[vdso]': 0x7ffff7ffa000,
            '[vsyscall]': 0xffffffffff600000
        }

if __name__ == '__main__':
    test_cat_ldd()
