# Runs in the finalizer AFTER the submission ODB was saved. The in-memory
# database below is timing-only: HBT buffers become explicit passive RC.
namespace eval mlsv2 {}
proc mlsv2::row {fp args} {
  foreach value $args {
    if {[regexp {[\t\r\n]} $value]} { error "Control character in feedback field" }
  }
  puts $fp [join $args "\t"]
}
proc mlsv2::ns {value} { expr {[sta::time_ui_sta $value]*1e9} }
proc mlsv2::collapse {base nets hbts} {
  set block [ord::get_db_block]
  foreach name $hbts {
    set inst [$block findInst $name]
    if {$inst eq "NULL"} { error "Missing timing HBT $name" }
    odb::dbInst_destroy $inst
  }
  set survivor [$block findNet [lindex $nets 0]]
  set existing [$block findNet $base]
  if {$existing ne "NULL" && [$existing getITermCount]+[$existing getBTermCount] != 0} {
    error "Nonempty original net coexists with HBT family $base"
  }
  if {$existing ne "NULL"} { odb::dbNet_destroy $existing }
  foreach name [lrange $nets 1 end] {
    set donor [$block findNet $name]
    if {$donor eq "NULL" || ![$survivor canMergeNet $donor]} { error "Cannot collapse $base" }
    $survivor mergeNet $donor
  }
  if {![$survivor rename $base]} { error "Cannot rename timing family $base" }
}
proc mlsv2::pin_info {pin} {
  set term [sta::sta_to_db_pin $pin]
  if {$term eq "NULL"} { return {} }
  set net [$term getNet]
  if {$net eq "NULL"} { return {} }
  if {![catch {$term getInst} inst] && $inst ne "NULL"} {
    set name "[$inst getName]:[[$term getMTerm] getName]"
  } else {
    set name [$term getName]
  }
  return [list [$net getName] $name]
}

proc mlsv2::snapshot {algorithm_dir} {
set results $::env(RESULTS_DIR)
set started [clock milliseconds]
puts [exec python3 $algorithm_dir/spef.py $results]
source $results/mls_collapse.tcl
read_spef $results/mls_merged.spef
sta::find_timing

set fp [open $results/mls_timing.tsv w]
mlsv2::row $fp SCHEMA 2
mlsv2::row $fp MODEL GRT_SPEF_passive_HBT_3ohm_0.6ff
mlsv2::row $fp METRIC tns_ns [mlsv2::ns [sta::total_negative_slack -max]]
mlsv2::row $fp METRIC wns_ns [mlsv2::ns [sta::worst_negative_slack -max]]
mlsv2::row $fp METRIC hold_wns_ns [mlsv2::ns [sta::worst_negative_slack -min]]
mlsv2::row $fp METRIC slew_violations [sta::max_slew_violation_count]
mlsv2::row $fp METRIC capacitance_violations [sta::max_capacitance_violation_count]
mlsv2::row $fp CONDITION clock_mode [expr {[info exists ::env(IDEAL_CLOCK)] ? "ideal" : "propagated"}]
mlsv2::row $fp CONDITION setup max
set endpoints [sta::endpoints]
foreach pin $endpoints {
  set slack [mlsv2::ns [get_property $pin slack_max]]
  # OpenSTA represents unconstrained endpoints by a very large finite value.
  if {abs($slack) < 1e20} { mlsv2::row $fp ENDPOINT [get_full_name $pin] $slack }
}
set count $::env(MLSV2_PATH_COUNT)
set per_endpoint $::env(MLSV2_ENDPOINT_PATH_COUNT)
set point_limit $::env(MLSV2_PATH_POINT_BUDGET)
set paths [find_timing_paths -path_delay max -group_path_count $count \
  -endpoint_path_count $per_endpoint -sort_by_slack \
  -slack_max [sta::time_sta_ui [expr {$::env(MLSV2_SLACK_MARGIN_NS)*1e-9}]]]
set path_id 0
set points_used 0
set truncated 0
foreach path $paths {
  set points [get_property $path points]
  if {$points_used + [llength $points] > $point_limit || $path_id >= $count} {
    set truncated 1
    break
  }
  incr points_used [llength $points]
  set endpoint [get_full_name [get_property $path endpoint]]
  mlsv2::row $fp PATH $path_id $endpoint [mlsv2::ns [get_property $path slack]]
  set previous {}
  foreach point $points {
    set pin [get_property $point pin]
    set info [mlsv2::pin_info $pin]
    set arrival [mlsv2::ns [get_property $point arrival]]
    if {[llength $info] && [llength $previous] && [lindex $info 0] eq [lindex $previous 0]} {
      mlsv2::row $fp BRANCH $path_id [lindex $info 0] [lindex $previous 1] \
        [lindex $info 1] [expr {$arrival-[lindex $previous 2]}]
    }
    set previous [expr {[llength $info] ? [concat $info [list $arrival]] : {}}]
  }
  incr path_id
}
mlsv2::row $fp SAMPLE paths $path_id
mlsv2::row $fp SAMPLE points $points_used
mlsv2::row $fp SAMPLE truncated [expr {$truncated || [llength $paths] >= $count}]
mlsv2::row $fp SAMPLE endpoint_path_limit $per_endpoint
close $fp

set fp [open $results/mls_rc.tsv w]
set tech [ord::get_db_tech]
foreach layer [$tech getLayers] {
  if {[$layer getRoutingLevel] > 0} {
    lassign [est::dblayer_wire_rc $layer] r c
    # The estimator returns SI ohm/m and F/m, independent of Liberty units.
    mlsv2::row $fp LAYER [$layer getName] [$layer getRoutingLevel] \
      [$layer getDirection] [expr {$r*1e-6}] [expr {$c*1e9}]
  } elseif {[$layer getType] eq "CUT"} {
    set lower [$layer getLowerLayer]
    set upper [$layer getUpperLayer]
    if {$lower ne "NULL" && $upper ne "NULL"} {
      mlsv2::row $fp VIA [$layer getName] [$layer getResistance] \
        [$lower getRoutingLevel] [$upper getRoutingLevel]
    }
  }
}
set missing 0
foreach pin [get_pins -hierarchical *] {
  set info [mlsv2::pin_info $pin]
  if {![llength $info]} { continue }
  set port [$pin liberty_port]
  if {$port eq "NULL"} { incr missing; continue }
  set cap [expr {[sta::capacitance_ui_sta [get_property $port capacitance]]*1e15}]
  set r [sta::resistance_ui_sta [get_property $port drive_resistance]]
  set slew [mlsv2::ns [get_property $pin slew_max]]
  mlsv2::row $fp PIN {*}$info $cap $r $slew
}
mlsv2::row $fp MISSING_LIBERTY_PINS $missing
close $fp
set fp [open $results/mls_snapshot_seconds w]
puts $fp [expr {([clock milliseconds]-$started)/1000.0}]
close $fp
puts "MLSv2: exported merged GRT timing and calibrated RC (sampled $path_id paths)"
}

if {![info exists ::env(MLSV2_SNAPSHOT_LIBRARY_ONLY)] || !$::env(MLSV2_SNAPSHOT_LIBRARY_ONLY)} {
  mlsv2::snapshot [file dirname [file normalize [info script]]]
}
