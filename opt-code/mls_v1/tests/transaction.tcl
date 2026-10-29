# Real OpenDB integration test. Set MLSV1_TEST_ODB and MLSV1_TEST_PLAN to a
# pre-MLS CTS ODB and a nonempty generated plan. Writes scratch reports only.
read_db $::env(MLSV1_TEST_ODB)
set ::env(MLSV1_LIBRARY_ONLY) 1
source [file join [file dirname [info script]] .. prepare.tcl]
set ::env(RESULTS_DIR) $::env(MLSV1_TEST_SCRATCH)
file mkdir $::env(RESULTS_DIR)
mlsv1::export_design $::env(RESULTS_DIR)/design.tsv

proc connectivity_snapshot {} {
  set state [dict create]
  foreach net [[ord::get_db_block] getNets] {
    dict set state [$net getName] [mlsv1::terminal_keys $net]
  }
  return $state
}
proc require {condition message} {
  if {![uplevel 1 [list expr $condition]]} { error $message }
}

set block [ord::get_db_block]
set placement [mlsv1::fixed_snapshot]
set connectivity [connectivity_snapshot]
set inst_count [llength [$block getInsts]]
set net_count [llength [$block getNets]]
set create_calls 0
rename ::odb::dbInst_create ::odb::mlsv1_real_create
proc ::odb::dbInst_create {args} {
  incr ::create_calls
  if {$::create_calls == 2} { return NULL }
  return [::odb::mlsv1_real_create {*}$args]
}
set failed [catch {source $::env(MLSV1_TEST_PLAN)} message]
rename ::odb::dbInst_create {}
rename ::odb::mlsv1_real_create ::odb::dbInst_create
require {$failed && [string match "*rolled back*" $message]} "Expected an injected transaction failure: $message"
require {$placement eq [mlsv1::fixed_snapshot]} "Placement changed after rollback"
set restored [connectivity_snapshot]
require {[dict size $connectivity] == [dict size $restored]} "Net count changed after rollback"
dict for {name terminals} $connectivity {
  require {[dict exists $restored $name] && $terminals eq [dict get $restored $name]} "Connectivity changed on $name"
}
require {$inst_count == [llength [$block getInsts]]} "HBT leaked after rollback"

source $::env(MLSV1_TEST_PLAN)
require {$mlsv1::committed == 1} "Test plan must commit exactly one MLS net"
require {$inst_count+2 == [llength [$block getInsts]]} "Must create exactly two HBTs"
require {$net_count+2 == [llength [$block getNets]]} "Must replace one net with three"
require {$placement eq [mlsv1::fixed_snapshot]} "Placement changed on successful commit"
puts "MLSV1_TRANSACTION_TEST_PASS"
