#!/usr/bin/env bash
# Prove each labelled property in pio_props.vh with Yosys' built-in SAT solver
# (BMC base case + k-induction, no external SMT solver needed).
#
# Usage: formal/run_formal.sh [path/to/project.v] [max_steps]
# Counterexample traces are written to formal/build/<property>.vcd
set -u

here=$(cd "$(dirname "$0")" && pwd)
src=$(realpath "${1:-$here/../src/project.v}")
steps=${2:-20}
build="$here/build"
mkdir -p "$build"

props=$(grep -oE '^\s*p_[a-z0-9_]+:' "$here/pio_props.vh" | tr -d ' :\r')
pass=0
fail=0
unknown=0

for p in $props; do
  log="$build/$p.log"
  rm -f "$build/$p.vcd"
  yosys -q -l "$log" -p "
    read_verilog -formal -DFORMAL -I $here $src
    prep -top tt_um_ryanalumkal
    async2sync
    memory_map
    opt_clean
    chformal -assert -remove t:\$assert n:$p %d
    sat -tempinduct -prove-asserts -set-init-zero -seq 1 -maxsteps $steps \
        -show-public -dump_vcd $build/$p.vcd
  " > /dev/null 2>&1

  if grep -q "^ERROR" "$log"; then
    printf "ERROR    %s (%s)\n" "$p" "$(grep -m1 '^ERROR' "$log")"
    unknown=$((unknown + 1))
  elif grep -q "Induction step proven: SUCCESS" "$log"; then
    printf "PASS     %s\n" "$p"
    pass=$((pass + 1))
  elif grep -q "model found for base case: FAIL" "$log"; then
    printf "FAIL     %s (counterexample: formal/build/%s.vcd)\n" "$p" "$p"
    fail=$((fail + 1))
  else
    printf "UNKNOWN  %s (base case holds for %s steps, induction did not close)\n" "$p" "$steps"
    unknown=$((unknown + 1))
  fi
done

echo "pass=$pass fail=$fail unknown=$unknown"
[ "$fail" -eq 0 ] && [ "$unknown" -eq 0 ]
