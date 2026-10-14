"""Apply resource ceilings after exec, avoiding preexec_fn in a threaded service."""
import os
import resource
import sys

def main():
    resource.setrlimit(resource.RLIMIT_FSIZE,(64*1024*1024,64*1024*1024))
    resource.setrlimit(resource.RLIMIT_NOFILE,(256,256))
    os.execv(sys.argv[1],sys.argv[1:])

if __name__=='__main__':main()
