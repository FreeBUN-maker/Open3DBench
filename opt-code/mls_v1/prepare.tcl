# Sourced by GRT_PREPARE_TCL after load_design. No detailed wires are created.
namespace eval mlsv1 {
  variable algorithm_dir [file dirname [file normalize [info script]]]
  variable committed 0
  variable next_id 0
  variable pitch
  variable grid
  variable origin
  variable occupied {}
  variable obstacles {}
  variable immutable
}

proc mlsv1::is_hbt {inst} {
  expr {[string match "HBT_*" [[$inst getMaster] getName]] ||
        [string match "HBT_*" [$inst getName]] ||
        [string match "LS_HBT_*" [$inst getName]]}
}

proc mlsv1::row {fp args} {
  foreach value $args {
    if {[regexp {[\t\r\n]} $value]} { error "TSV field contains a control character" }
  }
  puts $fp [join $args "\t"]
}

proc mlsv1::term_die {term} {
  set dies {}
  foreach mpin [[$term getMTerm] getMPins] {
    foreach shape [$mpin getGeometry] {
      set layer [$shape getTechLayer]
      if {$layer eq "NULL"} { continue }
      set name [$layer getName]
      if {![regexp {^metal([0-9]+)$} $name unused level]} { continue }
      if {$level >= 1 && $level <= 10} { lappend dies 0 }
      if {$level >= 11 && $level <= 20} { lappend dies 1 }
    }
  }
  set dies [lsort -unique $dies]
  if {[llength $dies] != 1} { return -1 }
  return [lindex $dies 0]
}

proc mlsv1::criticalities {} {
  set weights [dict create]
  if {[catch {
    estimate_parasitics -placement
    set paths [find_timing_paths -path_delay max -group_path_count 1000 \
      -endpoint_path_count 1 -sort_by_slack -slack_max 0.0]
  } message]} {
    puts "MLSv1: STA unavailable, using congestion/geometry ranking: $message"
    return $weights
  }
  foreach path $paths {
    set slack [get_property $path slack]
    set weight [expr {1.0-exp(min(0.0, double($slack)))}]
    foreach point [get_property $path points] {
      set term [sta::sta_to_db_pin [get_property $point pin]]
      if {$term eq "NULL" || [catch {$term getInst} inst] || $inst eq "NULL"} { continue }
      set key [list [$inst getName] [[$term getMTerm] getName]]
      if {![dict exists $weights $key] || [dict get $weights $key] < $weight} {
        dict set weights $key $weight
      }
    }
  }
  puts "MLSv1: sampled timing criticalities for [dict size $weights] pins"
  return $weights
}

proc mlsv1::fixed_snapshot {} {
  set result [dict create]
  foreach inst [[ord::get_db_block] getInsts] {
    if {![is_hbt $inst]} {
      dict set result [$inst getName] [list [[$inst getMaster] getName] \
        [$inst getOrigin] [$inst getOrient] [$inst getPlacementStatus]]
    }
  }
  # The algorithm never touches BTerms/BPins; include their geometry in the assertion.
  foreach term [[ord::get_db_block] getBTerms] {
    set geometry {}
    foreach pin [$term getBPins] {
      foreach box [$pin getBoxes] {
        lappend geometry [list [[$box getTechLayer] getName] [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
      }
    }
    dict set result [list PACKAGE_PIN [$term getName]] $geometry
  }
  return $result
}

proc mlsv1::export_design {path} {
  variable pitch
  variable grid
  variable origin
  variable occupied
  variable obstacles
  set block [ord::get_db_block]
  set tech [ord::get_db_tech]
  set db [ord::get_db]
  set dbu [$tech getDbUnitsPerMicron]
  set pitch [expr {int(round(6.4*$dbu))}]
  set grid [$tech getManufacturingGrid]
  if {$grid <= 0} { error "Missing manufacturing grid" }
  set master [$db findMaster HBT_BOTIN]
  if {$master eq "NULL" || [$db findMaster HBT_TOPIN] eq "NULL"} { error "Missing HBT masters" }
  set weights [criticalities]
  set fp [open $path w]
  row $fp DBU $dbu
  set box [$block getDieArea]
  row $fp BOUNDS [$box xMin] [$box yMin] [$box xMax] [$box yMax]
  set top_master [$db findMaster HBT_TOPIN]
  row $fp HBT $pitch $grid [expr {max([$master getWidth], [$top_master getWidth])}] \
    [expr {max([$master getHeight], [$top_master getHeight])}]
  foreach layer [$tech getLayers] {
    if {![regexp {^metal([0-9]+)$} [$layer getName] unused level] || $level < 2 || $level > 20} { continue }
    set direction [$layer getDirection]
    set spacing [expr {$direction eq "HORIZONTAL" ? [$layer getPitchY] : [$layer getPitchX]}]
    if {$spacing <= 0} { continue }
    row $fp TRACK [expr {$level <= 10 ? 0 : 1}] $direction $spacing
  }
  foreach inst [$block getInsts] {
    if {[is_hbt $inst]} {
      lassign [$inst getOrigin] x y
      lappend occupied [list $x $y]
      row $fp EXISTING $x $y
    } elseif {[[$inst getMaster] getType] eq "BLOCK"} {
      set box [$inst getBBox]
      set coords [list [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
      lappend obstacles $coords
      row $fp OBSTACLE {*}$coords
    }
  }
  if {[llength $occupied] == 0} { close $fp; error "Cannot infer an HBT lattice" }
  lassign [lindex $occupied 0] x y
  set origin [list [expr {(($x % $pitch)+$pitch)%$pitch}] [expr {(($y % $pitch)+$pitch)%$pitch}]]
  set exported 0
  foreach net [$block getNets] {
    if {[$net getSigType] ne "SIGNAL" || [$net getBTermCount] != 0 || [$net getITermCount] < 2} { continue }
    set pins {}
    set dies {}
    set eligible 1
    set drivers 0
    foreach term [$net getITerms] {
      set inst [$term getInst]
      set die [term_die $term]
      lassign [$term getAvgXY] valid x y
      if {!$valid || $die < 0} { set pins {}; break }
      lappend dies $die
      set direction [$term getIoType]
      if {$direction eq "OUTPUT"} { incr drivers }
      if {[is_hbt $inst] || [[$inst getMaster] getType] eq "BLOCK" ||
          $direction ni {INPUT OUTPUT}} { set eligible 0 }
      set key [list [$inst getName] [[$term getMTerm] getName]]
      set crit [expr {[dict exists $weights $key] ? [dict get $weights $key] : 0.0}]
      lappend pins [list PIN [$net getName] {*}$key $x $y $direction $crit]
    }
    set dies [lsort -unique $dies]
    if {[llength $pins] < 2 || [llength $dies] != 1} { continue }
    set die [lindex $dies 0]
    set name [$net getName]
    if {$die != 0 || $drivers != 1 || [string match "*__MLS__*" $name] ||
        [string match "*_BOT" $name] || [string match "*_TOP" $name]} { set eligible 0 }
    row $fp NET $name $die $eligible
    foreach pin $pins { row $fp {*}$pin }
    incr exported
  }
  close $fp
  puts "MLSv1: exported $exported die-local signal nets to $path"
}

proc mlsv1::site_legal {x y width height} {
  variable pitch
  variable grid
  variable origin
  variable occupied
  variable obstacles
  if {$x % $grid || $y % $grid || ($x-[lindex $origin 0]) % $pitch ||
      ($y-[lindex $origin 1]) % $pitch} { return 0 }
  set box [[ord::get_db_block] getDieArea]
  if {$x < [$box xMin] || $y < [$box yMin] ||
      $x+$width > [$box xMax] || $y+$height > [$box yMax]} { return 0 }
  foreach xy $occupied {
    lassign $xy ox oy
    if {max(abs($x-$ox), abs($y-$oy)) < $pitch} { return 0 }
  }
  foreach box $obstacles {
    lassign $box lx ly ux uy
    if {$x < $ux && $x+$width > $lx && $y < $uy && $y+$height > $ly} { return 0 }
  }
  return 1
}

proc mlsv1::terminal_keys {net {exclude_hbt 0}} {
  set keys {}
  foreach term [$net getITerms] {
    if {$exclude_hbt && [is_hbt [$term getInst]]} { continue }
    lappend keys [list [[$term getInst] getName] [[$term getMTerm] getName]]
  }
  return [lsort $keys]
}

proc mlsv1::commit {name sink_keys ax ay bx by} {
  variable occupied
  variable committed
  variable next_id
  variable pitch
  set block [ord::get_db_block]
  set db [ord::get_db]
  set net [$block findNet $name]
  if {$net eq "NULL"} { error "Missing planned net $name" }
  set original [terminal_keys $net]
  if {[$net getSigType] ne "SIGNAL" || [$net getBTermCount] != 0 ||
      [llength $sink_keys] == 0 || [llength $sink_keys] >= [llength $original]} {
    error "Invalid source/sink partition for $name"
  }
  set sinks {}
  foreach key $sink_keys {
    lassign $key inst port
    set object [$block findInst $inst]
    if {$object eq "NULL"} { error "Missing planned instance $inst" }
    set term [$object findITerm $port]
    if {$term eq "NULL" || [$term getNet] ne $net || [$term getIoType] ne "INPUT"} {
      error "Invalid sink partition for $name: $key"
    }
    lappend sinks $term
  }
  if {[llength [lsort -unique $sink_keys]] != [llength $sink_keys]} { error "Duplicate sink" }
  foreach term [$net getITerms] {
    if {[is_hbt [$term getInst]] || [term_die $term] != 0} { error "Not an original bottom 2D net: $name" }
  }
  set m0 [$db findMaster HBT_BOTIN]
  set m1 [$db findMaster HBT_TOPIN]
  foreach master [list $m0 $m1] x [list $ax $bx] y [list $ay $by] {
    if {![site_legal $x $y [$master getWidth] [$master getHeight]]} { error "Illegal planned HBT site on $name" }
  }
  if {max(abs($ax-$bx), abs($ay-$by)) < $pitch} { error "HBT pair conflicts on $name" }
  set names [list ${name}__MLS__S0__BOT ${name}__MLS__S1__TOP ${name}__MLS__S2__BOT]
  foreach n $names { if {[$block findNet $n] ne "NULL"} { error "Subnet name collision: $n" } }
  set hbt_names {}
  while {[llength $hbt_names] < 2} {
    set n LS_HBT_$next_id
    incr next_id
    if {[$block findInst $n] eq "NULL"} { lappend hbt_names $n }
  }
  set created_nets {}
  set created_insts {}
  set renamed 0
  set changed_sinks {}
  if {[catch {
    if {![$net rename [lindex $names 0]]} { error "Cannot rename $name" }
    set renamed 1
    foreach n [lrange $names 1 2] {
      set object [odb::dbNet_create $block $n]
      if {$object eq "NULL"} { error "Cannot create $n" }
      lappend created_nets $object
      $object setSigType SIGNAL
    }
    lassign $created_nets shared terminal
    foreach term $sinks {
      lappend changed_sinks $term
      $term disconnect
      $term connect $terminal
    }
    foreach master [list $m0 $m1] hname $hbt_names x [list $ax $bx] y [list $ay $by] \
            bottom [list $net $terminal] {
      set inst [odb::dbInst_create $block $master $hname]
      if {$inst eq "NULL"} { error "Cannot create $hname" }
      lappend created_insts $inst
      $inst setOrient R0
      $inst setLocation $x $y
      $inst setPlacementStatus COVER
      [$inst findITerm BOT] connect $bottom
      [$inst findITerm TOP] connect $shared
    }
    # Collapsing the two HBTs yields exactly one chain and the original pins.
    set actual [concat [terminal_keys $net 1] [terminal_keys $terminal 1]]
    if {[lsort $actual] ne $original || [$shared getITermCount] != 2 ||
        [llength $created_insts] != 2 || [$terminal getITermCount] != [llength $sinks]+1} {
      error "Collapsed logical netlist mismatch on $name"
    }
    foreach inst $created_insts bottom [list $net $terminal] {
      if {[[$inst findITerm BOT] getNet] ne $bottom ||
          [[$inst findITerm TOP] getNet] ne $shared} { error "Invalid HBT chain" }
    }
  } message options]} {
    foreach inst [lreverse $created_insts] { odb::dbInst_destroy $inst }
    foreach term $changed_sinks { $term disconnect; $term connect $net }
    foreach object [lreverse $created_nets] { odb::dbNet_destroy $object }
    if {$renamed && ![$net rename $name]} { error "Rollback rename failed on $name" }
    error "MLS transaction rolled back on $name: $message"
  }
  lappend occupied [list $ax $ay] [list $bx $by]
  incr committed
  set fp [open $::env(RESULTS_DIR)/mls_commits.tsv a]
  row $fp $name {*}$names {*}$hbt_names
  close $fp
  puts "MLSv1: committed $name, sinks=[llength $sinks], HBTs=$hbt_names"
}

proc mlsv1::prepare {} {
  variable algorithm_dir
  variable immutable
  variable committed
  set immutable [fixed_snapshot]
  set results $::env(RESULTS_DIR)
  file delete -force $results/mls_commits.tsv
  export_design $results/mls_design.tsv
  set config $algorithm_dir/config.json
  if {[info exists ::env(MLSV1_CONFIG)]} { set config $::env(MLSV1_CONFIG) }
  set cmd [list python3 $algorithm_dir/planner.py --design $results/mls_design.tsv --config $config --output $results]
  if {[info exists ::env(MLSV1_MAX_NETS)]} { lappend cmd --max-nets $::env(MLSV1_MAX_NETS) }
  puts [exec {*}$cmd]
  source $results/mls_plan.tcl
  if {[fixed_snapshot] ne $immutable} { error "Non-HBT placement or package-pin geometry changed" }
  puts "MLSv1: $committed net transactions committed; fixed placements preserved"
}

if {![info exists ::env(MLSV1_LIBRARY_ONLY)] || !$::env(MLSV1_LIBRARY_ONLY)} {
  mlsv1::prepare
}
