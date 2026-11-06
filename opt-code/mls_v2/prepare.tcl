# Library-only reuse of the v1 OpenDB transaction and immutable-geometry checks.
set ::env(MLSV1_LIBRARY_ONLY) 1
source [file join [file dirname [info script]] ../mls_v1/prepare.tcl]
proc mlsv1::criticalities {} { return [dict create] }

namespace eval mlsv2 {}
proc mlsv2::move_hbt {name x y} {
  set inst [[ord::get_db_block] findInst $name]
  if {$inst eq "NULL" || ![mlsv1::is_hbt $inst]} { error "Missing HBT $name" }
  set old [$inst getOrigin]
  set saved $::mlsv1::occupied
  set index [lsearch -exact $saved $old]
  if {$index < 0} { error "Missing occupied site for $name" }
  set ::mlsv1::occupied [lreplace $saved $index $index]
  set master [$inst getMaster]
  if {![mlsv1::site_legal $x $y [$master getWidth] [$master getHeight]]} {
    set ::mlsv1::occupied $saved
    error "Illegal relocation for $name"
  }
  if {[catch {$inst setOrigin $x $y} message]} {
    $inst setOrigin {*}$old
    set ::mlsv1::occupied $saved
    error "HBT relocation rolled back: $message"
  }
  lappend ::mlsv1::occupied [list $x $y]
}

set mlsv2_prepare_started [clock milliseconds]
set immutable [mlsv1::fixed_snapshot]
file delete -force $::env(RESULTS_DIR)/mls_commits.tsv
mlsv1::export_design $::env(RESULTS_DIR)/mls_original.tsv
if {![info exists ::env(MLSV2_PLAN_TCL)]} { error "MLSV2_PLAN_TCL is required" }
source $::env(MLSV2_PLAN_TCL)
if {[mlsv1::fixed_snapshot] ne $immutable} { error "Immutable geometry changed" }
set ::mlsv1::occupied {}
set ::mlsv1::obstacles {}
mlsv1::export_design $::env(RESULTS_DIR)/mls_design.tsv
set fp [open $::env(RESULTS_DIR)/mls_hbts.tsv w]
foreach inst [[ord::get_db_block] getInsts] {
  if {![mlsv1::is_hbt $inst]} { continue }
  set bot [[$inst findITerm BOT] getNet]
  set top [[$inst findITerm TOP] getNet]
  if {$bot eq "NULL" || $top eq "NULL"} { error "Unconnected HBT" }
  mlsv1::row $fp [$inst getName] [$bot getName] [$top getName] {*}[$inst getOrigin]
}
close $fp

# Record actual transformed pin boxes and routing obstructions separately from
# the conservative HBT macro keepouts. These are evidence, not a DRC engine.
set fp [open $::env(RESULTS_DIR)/mls_geometry.tsv w]
foreach obs [[ord::get_db_block] getObstructions] {
  set box [$obs getBBox]
  mlsv1::row $fp OBS [[$box getTechLayer] getName] [$box xMin] [$box yMin] [$box xMax] [$box yMax]
}
foreach net [[ord::get_db_block] getNets] {
  if {[$net getNonDefaultRule] ne "NULL"} { mlsv1::row $fp NDR [$net getName] }
  foreach term [$net getITerms] {
    set box [$term getBBox]
    mlsv1::row $fp PIN [$net getName] [[$term getInst] getName] \
      [[$term getMTerm] getName] [$box xMin] [$box yMin] [$box xMax] [$box yMax]
    foreach mpin [[$term getMTerm] getMPins] {
      foreach shape [$mpin getGeometry] {
        set layer [$shape getTechLayer]
        if {$layer ne "NULL" && [$layer getRoutingLevel] > 0} {
          mlsv1::row $fp PIN_LAYER "[[$term getInst] getName]:[[$term getMTerm] getName]" [$layer getRoutingLevel]
        }
      }
    }
  }
}
close $fp
puts "MLSv2: prepared replayable solution; immutable geometry preserved"
set fp [open $::env(RESULTS_DIR)/mls_prepare_seconds w]
puts $fp [expr {([clock milliseconds]-$mlsv2_prepare_started)/1000.0}]
close $fp
