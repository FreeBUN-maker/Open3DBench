# API/geometry smoke check only: no GRT, DRT, candidate search or submission.
# Requires MLSV2_TEST_ODB, MLSV2_TEST_PLATFORM and MLSV2_TEST_SCRATCH.
set platform $::env(MLSV2_TEST_PLATFORM)
foreach die {bottom upper} {
  read_liberty $platform/lib_${die}/NangateOpenCellLibrary_typical.${die}.lib
}
read_db $::env(MLSV2_TEST_ODB)
source $platform/setRC.tcl
set ::env(RESULTS_DIR) $::env(MLSV2_TEST_SCRATCH)
file mkdir $::env(RESULTS_DIR)
set ::env(MLSV2_PLAN_TCL) $::env(RESULTS_DIR)/empty.tcl
close [open $::env(MLSV2_PLAN_TCL) w]
set algorithm_dir [file normalize [file join [file dirname [info script]] ..]]
source $algorithm_dir/prepare.tcl
set ::env(MLSV2_SNAPSHOT_LIBRARY_ONLY) 1
source $algorithm_dir/snapshot.tcl
set tested 0
foreach pin [get_pins -hierarchical *] {
  set port [$pin liberty_port]
  if {$port eq "NULL"} { continue }
  mlsv2::pin_info $pin
  sta::capacitance_ui_sta [get_property $port capacitance]
  sta::resistance_ui_sta [get_property $port drive_resistance]
  incr tested
  if {$tested >= 10} { break }
}
if {$tested != 10} { error "Insufficient Liberty pins for API check" }
foreach layer [[ord::get_db_tech] getLayers] {
  if {[$layer getRoutingLevel] > 0} {
    est::dblayer_wire_rc $layer
  } elseif {[$layer getType] eq "CUT"} {
    $layer getLowerLayer
    $layer getUpperLayer
  }
}
# Verify no-ops and rollback of an illegal existing-HBT move preserve occupancy.
set name [lindex [split [lindex [split [read [set fp [open $::env(RESULTS_DIR)/mls_hbts.tsv]]] "\n"] 0] "\t"] 0]
close $fp
set inst [[ord::get_db_block] findInst $name]
set xy [$inst getOrigin]
set occupied $::mlsv1::occupied
mlsv2::move_hbt $name {*}$xy
if {[$inst getOrigin] ne $xy} { error "HBT no-op relocation changed origin" }
if {![catch {mlsv2::move_hbt $name -999999 -999999}]} { error "Invalid HBT relocation accepted" }
if {[$inst getOrigin] ne $xy || [lsort $::mlsv1::occupied] ne [lsort $occupied]} { error "Relocation rollback failed" }
puts "MLSV2_API_SMOKE_PASS"
