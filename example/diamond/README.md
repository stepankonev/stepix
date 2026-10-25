# Diamond: A, then B and C, then D

B and C both require only A, and D requires both B and C:

```
    a
   / \
  b   c
   \ /
    d
```

B and C each take 3 seconds and print when they start and end. Neither requires the other, so
they run at the same time: both start as soon as A ends, D starts once both have ended, and the
whole run takes about 4 seconds instead of 7.

```bash
uvx stepix run example/diamond/README.md
```

```stepix
[params]
seconds = 3
```

### A: the start

```stepix
[steps.a]
run = "echo a start $(date +%T); sleep 1; echo ::stepix::output value=from-a; echo a end $(date +%T)"
```

### B: requires A

```stepix
[steps.b]
run = "echo b start $(date +%T) got {steps.a.value}; sleep {params.seconds}; echo ::stepix::output value=from-b; echo b end $(date +%T)"
requires = ["a"]
```

### C: requires A

```stepix
[steps.c]
run = "echo c start $(date +%T) got {steps.a.value}; sleep {params.seconds}; echo ::stepix::output value=from-c; echo c end $(date +%T)"
requires = ["a"]
```

### D: requires B and C

```stepix
[steps.d]
run = "echo d start $(date +%T) got {steps.b.value} and {steps.c.value}; echo d end $(date +%T)"
requires = ["b", "c"]
```
