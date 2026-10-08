#!/usr/bin/env python3
"""Exec a command under a Landlock TCP-connect deny-by-default policy.

Allow selected destination TCP ports with LANDLOCK_ALLOW_CONNECT (comma-separated).
This inheritance is automatic for all child processes. It does not restrict UDP.
"""
import ctypes
import os
import sys

SYSCALL_LANDLOCK_CREATE_RULESET = 444
SYSCALL_LANDLOCK_ADD_RULE = 445
SYSCALL_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_NET_PORT = 2
LANDLOCK_ACCESS_NET_CONNECT_TCP = 1 << 1
PR_SET_NO_NEW_PRIVS = 38


class RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class NetPortAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("port", ctypes.c_uint64),
    ]


def syscall(number, *args):
    result = ctypes.CDLL(None, use_errno=True).syscall(ctypes.c_long(number), *args)
    if result == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return result


def allow_connect_ports(fd, ports):
    for port in ports:
        attr = NetPortAttr(LANDLOCK_ACCESS_NET_CONNECT_TCP, int(port))
        syscall(
            SYSCALL_LANDLOCK_ADD_RULE,
            ctypes.c_int(fd),
            ctypes.c_int(LANDLOCK_RULE_NET_PORT),
            ctypes.byref(attr),
            ctypes.c_uint(0),
        )


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        raise SystemExit("usage: landlock-exec.py -- COMMAND [ARG ...]")
    allow = [p for p in os.environ.get("LANDLOCK_ALLOW_CONNECT", "").split(",") if p]
    attr = RulesetAttr(0, LANDLOCK_ACCESS_NET_CONNECT_TCP, 0)
    fd = syscall(
        SYSCALL_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)),
        ctypes.c_uint(0),
    )
    allow_connect_ports(fd, allow)
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    syscall(SYSCALL_LANDLOCK_RESTRICT_SELF, ctypes.c_int(fd), ctypes.c_uint(0))
    os.close(fd)
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
