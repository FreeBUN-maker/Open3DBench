# Timing-driven metal-layer sharing for bp_fe.
# Sourced after load_design by the public die-by-die GRT flow.

namespace eval tmls {
  variable pitch
  variable origin_x
  variable origin_y
  variable occupied
  variable density
  variable macro_boxes
  variable bounds
  variable hbt_width
  variable hbt_height
  variable criticality
  variable path_hits
  variable worst_slack
  variable pin_criticality
  variable pin_path_hits
  variable pin_worst_slack
}

proc tmls::env_int {name default min_value max_value} {
  set value $default
  if {[info exists ::env($name)] && $::env($name) ne ""} {
    set value [expr {int($::env($name))}]
  }
  if {$value < $min_value || $value > $max_value} {
    error "$name must be between $min_value and $max_value"
  }
  return $value
}

proc tmls::env_double {name default min_value max_value} {
  set value $default
  if {[info exists ::env($name)] && $::env($name) ne ""} {
    set value [expr {double($::env($name))}]
  }
  if {$value < $min_value || $value > $max_value} {
    error "$name must be between $min_value and $max_value"
  }
  return $value
}

proc tmls::bottom_pin_xy {iterm} {
  set inst [$iterm getInst]
  set master [$inst getMaster]
  if {![string match "*_bottom" [$master getName]] ||
      [$master getType] eq "BLOCK"} {
    return {}
  }
  lassign [$iterm getAvgXY] valid x y
  if {!$valid} {
    return {}
  }
  return [list $x $y]
}

proc tmls::pin_key {iterm} {
  return "[[$iterm getInst] getName]/[[$iterm getMTerm] getName]"
}

proc tmls::mark_site {ix iy} {
  variable occupied
  for {set dx -1} {$dx <= 1} {incr dx} {
    for {set dy -1} {$dy <= 1} {incr dy} {
      set occupied([expr {$ix + $dx}],[expr {$iy + $dy}]) 1
    }
  }
}

proc tmls::site_density {ix iy} {
  variable density
  if {[info exists density($ix,$iy)]} {
    return $density($ix,$iy)
  }
  return 0
}

proc tmls::site_available {ix iy} {
  variable occupied
  variable macro_boxes
  variable bounds
  variable origin_x
  variable origin_y
  variable pitch
  variable hbt_width
  variable hbt_height

  if {[info exists occupied($ix,$iy)]} {
    return 0
  }
  set x [expr {$origin_x + $ix * $pitch}]
  set y [expr {$origin_y + $iy * $pitch}]
  lassign $bounds lx ly ux uy
  if {$x < $lx || $y < $ly ||
      $x + $hbt_width > $ux || $y + $hbt_height > $uy} {
    return 0
  }
  foreach box $macro_boxes {
    lassign $box mx0 my0 mx1 my1
    if {$x < $mx1 && $x + $hbt_width > $mx0 &&
        $y < $my1 && $y + $hbt_height > $my0} {
      return 0
    }
  }
  return 1
}

proc tmls::nearest_site {x y {other_site {}}} {
  variable origin_x
  variable origin_y
  variable pitch

  set cx [expr {int(round(double($x - $origin_x) / $pitch))}]
  set cy [expr {int(round(double($y - $origin_y) / $pitch))}]
  set best {}
  set best_cost 1e30
  for {set radius 0} {$radius <= 10} {incr radius} {
    for {set dx [expr {-$radius}]} {$dx <= $radius} {incr dx} {
      for {set dy [expr {-$radius}]} {$dy <= $radius} {incr dy} {
        if {[expr {max(abs($dx), abs($dy))}] != $radius} {
          continue
        }
        set ix [expr {$cx + $dx}]
        set iy [expr {$cy + $dy}]
        if {![site_available $ix $iy]} {
          continue
        }
        if {$other_site ne {}} {
          set ox [lindex $other_site 0]
          set oy [lindex $other_site 1]
          if {abs($ix - $ox) + abs($iy - $oy) < 8} {
            continue
          }
        }
        set sx [expr {$origin_x + $ix * $pitch}]
        set sy [expr {$origin_y + $iy * $pitch}]
        set stub [expr {double(abs($sx - $x) + abs($sy - $y)) / $pitch}]
        set demand [site_density $ix $iy]
        set cost [expr {$stub + 0.35 * $demand}]
        if {$cost < $best_cost} {
          set best_cost $cost
          set best [list $ix $iy $sx $sy $stub $demand]
        }
      }
    }
    if {$best ne {} && $radius >= 3} {
      break
    }
  }
  return $best
}

proc tmls::find_site_pair {ax ay bx by} {
  set source [nearest_site $ax $ay]
  if {$source eq {}} {
    return {}
  }
  set sink [nearest_site $bx $by $source]
  if {$sink eq {}} {
    return {}
  }
  return [list $source $sink]
}

proc tmls::add_hbt {block master name site bottom_net top_net} {
  lassign $site ix iy x y stub demand
  set inst [odb::dbInst_create $block $master $name]
  if {$inst eq "NULL"} {
    error "Cannot create $name"
  }
  $inst setLocation $x $y
  $inst setPlacementStatus COVER
  [$inst findITerm BOT] connect $bottom_net
  [$inst findITerm TOP] connect $top_net
  mark_site $ix $iy
}

proc tmls::collect_timing_criticality {path_count} {
  variable criticality
  variable path_hits
  variable worst_slack
  variable pin_criticality
  variable pin_path_hits
  variable pin_worst_slack

  catch {array unset criticality}
  catch {array unset path_hits}
  catch {array unset worst_slack}
  catch {array unset pin_criticality}
  catch {array unset pin_path_hits}
  catch {array unset pin_worst_slack}

  if {[catch {estimate_parasitics -placement} message]} {
    puts "TMLS WARN: placement parasitic estimation failed: $message"
  }
  if {[catch {
    set paths [find_timing_paths -path_delay max -group_path_count $path_count \
      -endpoint_path_count 1 -sort_by_slack -slack_max 0.0]
  } message]} {
    puts "TMLS WARN: timing path extraction failed: $message"
    return 0
  }

  set used_paths 0
  foreach path_end $paths {
    set slack [expr {double([get_property $path_end slack])}]
    if {$slack >= 0.0} {
      continue
    }
    set severity [expr {min(10.0, -$slack)}]
    set weight [expr {1.0 + $severity}]
    array unset seen
    array unset seen_pin
    foreach point [get_property $path_end points] {
      set sta_pin [get_property $point pin]
      set db_pin [sta::sta_to_db_pin $sta_pin]
      if {$db_pin eq "NULL"} {
        continue
      }
      set net [$db_pin getNet]
      if {$net eq "NULL"} {
        continue
      }
      set name [$net getName]
      set key [pin_key $db_pin]
      if {![info exists seen_pin($key)]} {
        set seen_pin($key) 1
        if {![info exists pin_criticality($key)]} {
          set pin_criticality($key) 0.0
          set pin_path_hits($key) 0
          set pin_worst_slack($key) $slack
        }
        set pin_criticality($key) [expr {$pin_criticality($key) + $weight}]
        incr pin_path_hits($key)
        if {$slack < $pin_worst_slack($key)} {
          set pin_worst_slack($key) $slack
        }
      }
      if {[info exists seen($name)]} {
        continue
      }
      set seen($name) 1
      if {![info exists criticality($name)]} {
        set criticality($name) 0.0
        set path_hits($name) 0
        set worst_slack($name) $slack
      }
      set criticality($name) [expr {$criticality($name) + $weight}]
      incr path_hits($name)
      if {$slack < $worst_slack($name)} {
        set worst_slack($name) $slack
      }
    }
    incr used_paths
  }
  puts "TMLS timing paths: requested=$path_count negative_slack=$used_paths nets=[array size criticality]"
  return $used_paths
}

proc tmls::prepare {} {
  variable pitch
  variable origin_x
  variable origin_y
  variable macro_boxes
  variable bounds
  variable hbt_width
  variable hbt_height
  variable density
  variable criticality
  variable path_hits
  variable worst_slack
  variable pin_criticality
  variable pin_path_hits
  variable pin_worst_slack

  set block [ord::get_db_block]
  set db [ord::get_db]
  set tech [ord::get_db_tech]
  set dbu [$tech getDbUnitsPerMicron]
  set pitch [expr {int(round(6.4 * $dbu))}]
  set max_nets [env_int TMLS_MAX_NETS 0 0 16]
  set path_count [env_int TMLS_PATH_COUNT 2000 1 5000]
  set min_span_um [env_double TMLS_MIN_SPAN_UM 50.0 0.0 10000.0]
  set max_fanout [env_int TMLS_MAX_FANOUT 32 1 1024]
  set target_net [expr {[info exists ::env(TMLS_TARGET_NET)] ? $::env(TMLS_TARGET_NET) : ""}]

  set bottom_master [$db findMaster HBT_BOTIN]
  set top_master [$db findMaster HBT_TOPIN]
  if {$bottom_master eq "NULL" || $top_master eq "NULL"} {
    error "Missing HBT_BOTIN or HBT_TOPIN master"
  }
  set hbt_width [$bottom_master getWidth]
  set hbt_height [$bottom_master getHeight]
  set die [$block getDieArea]
  set bounds [list [$die xMin] [$die yMin] [$die xMax] [$die yMax]]

  set macro_boxes {}
  set first_hbt {}
  foreach inst [$block getInsts] {
    set name [$inst getName]
    set master [$inst getMaster]
    if {[string match "HBT_*" $name] || [string match "LS_HBT_*" $name]} {
      if {$first_hbt eq {}} {
        set first_hbt [$inst getOrigin]
      }
    } elseif {[$master getType] eq "BLOCK"} {
      set box [$inst getBBox]
      lappend macro_boxes [list [$box xMin] [$box yMin] [$box xMax] [$box yMax]]
    }
  }
  if {$first_hbt eq {}} {
    error "Cannot infer HBT lattice without an existing HBT"
  }
  lassign $first_hbt fx fy
  set origin_x [expr {(($fx % $pitch) + $pitch) % $pitch}]
  set origin_y [expr {(($fy % $pitch) + $pitch) % $pitch}]

  catch {array unset density}
  foreach inst [$block getInsts] {
    set name [$inst getName]
    set master [$inst getMaster]
    if {[string match "HBT_*" $name] || [string match "LS_HBT_*" $name]} {
      lassign [$inst getOrigin] x y
      mark_site [expr {($x - $origin_x) / $pitch}] \
                [expr {($y - $origin_y) / $pitch}]
      continue
    }
    if {[$master getType] eq "BLOCK" || ![string match "*_bottom" [$master getName]]} {
      continue
    }
    set box [$inst getBBox]
    set x [expr {([$box xMin] + [$box xMax]) / 2}]
    set y [expr {([$box yMin] + [$box yMax]) / 2}]
    set ix [expr {int(round(double($x - $origin_x) / $pitch))}]
    set iy [expr {int(round(double($y - $origin_y) / $pitch))}]
    for {set dx -1} {$dx <= 1} {incr dx} {
      for {set dy -1} {$dy <= 1} {incr dy} {
        set key "[expr {$ix + $dx}],[expr {$iy + $dy}]"
        if {![info exists density($key)]} {
          set density($key) 0
        }
        incr density($key)
      }
    }
  }

  collect_timing_criticality $path_count

  set candidates {}
  foreach net [$block getNets] {
    set name [$net getName]
    if {$target_net ne "" && $name ne $target_net} {
      continue
    }
    set fanout [expr {[$net getITermCount] - 1}]
    if {[$net getSigType] ne "SIGNAL" || [$net getBTermCount] != 0 ||
        $fanout < 1 || $fanout > $max_fanout ||
        [string match "*__MLS__*" $name] ||
        [string match "*_BOT" $name] || [string match "*_TOP" $name]} {
      continue
    }
    set driver {}
    set sinks {}
    foreach term [$net getITerms] {
      if {[$term getIoType] eq "OUTPUT"} {
        if {$driver ne {}} {
          set driver multiple
          break
        }
        set driver $term
      } elseif {[$term getIoType] eq "INPUT"} {
        lappend sinks $term
      }
    }
    if {$driver eq {} || $driver eq "multiple" || [llength $sinks] == 0} {
      continue
    }
    set a [bottom_pin_xy $driver]
    if {$a eq {}} {
      continue
    }
    lassign $a ax ay
    foreach sink $sinks {
      set b [bottom_pin_xy $sink]
      if {$b eq {}} {
        continue
      }
      lassign $b bx by
      set span [expr {abs($ax - $bx) + abs($ay - $by)}]
      set span_um [expr {double($span) / $dbu}]
      if {$span_um < $min_span_um} {
        continue
      }
      set sink_key [pin_key $sink]
      set crit [expr {[info exists pin_criticality($sink_key)] ? $pin_criticality($sink_key) : 0.0}]
      set hits [expr {[info exists pin_path_hits($sink_key)] ? $pin_path_hits($sink_key) : 0}]
      set slack [expr {[info exists pin_worst_slack($sink_key)] ? $pin_worst_slack($sink_key) : 0.0}]
      if {$target_net eq "" && $hits == 0} {
        continue
      }
      set pair [find_site_pair $ax $ay $bx $by]
      if {$pair eq {}} {
        continue
      }
      lassign $pair source_site sink_site
      set stub_um [expr {([lindex $source_site 4] + [lindex $sink_site 4]) * 6.4}]
      set demand [expr {[lindex $source_site 5] + [lindex $sink_site 5]}]
      set score [expr {500.0 * $crit + 100.0 * $hits + $span_um -
                       2.0 * $stub_um - 5.0 * $demand - 2.0 * $fanout}]
      lappend candidates [list $score $crit $hits $slack $span_um $stub_um \
                               $demand $fanout $name $sink_key $ax $ay $bx $by]
    }
  }
  set candidates [lsort -decreasing -real -index 0 $candidates]

  set report_path $::env(RESULTS_DIR)/timing_mls_candidates.tsv
  set report [open $report_path w]
  puts $report "rank\tscore\tcriticality\tpath_hits\tworst_slack_ns\tspan_um\tstub_um\tdensity\tfanout\tnet\tsink"
  set rank 0
  foreach candidate $candidates {
    incr rank
    lassign $candidate score crit hits slack span_um stub_um demand fanout name sink_key ax ay bx by
    puts $report [format "%d\t%.3f\t%.3f\t%d\t%.6f\t%.3f\t%.3f\t%d\t%d\t%s\t%s" \
      $rank $score $crit $hits $slack $span_um $stub_um $demand $fanout $name $sink_key]
    if {$rank <= 20} {
      puts [format "TMLS candidate %d: %s -> %s score=%.3f paths=%d worst=%.4f span=%.1fum fanout=%d" \
        $rank $name $sink_key $score $hits $slack $span_um $fanout]
    }
  }
  close $report

  set selected 0
  set rejected 0
  foreach candidate $candidates {
    if {$selected >= $max_nets} {
      break
    }
    lassign $candidate score crit hits slack span_um stub_um demand fanout name sink_key ax ay bx by
    set pair [find_site_pair $ax $ay $bx $by]
    if {$pair eq {}} {
      incr rejected
      continue
    }
    lassign $pair source_site sink_site
    set net [$block findNet $name]
    if {$net eq "NULL"} {
      incr rejected
      continue
    }
    set driver {}
    set sink {}
    foreach term [$net getITerms] {
      if {[$term getIoType] eq "OUTPUT"} {
        set driver $term
      } elseif {[$term getIoType] eq "INPUT" && [pin_key $term] eq $sink_key} {
        set sink $term
      }
    }
    if {$driver eq {} || $sink eq {}} {
      incr rejected
      continue
    }
    set base "${name}__MLS__"
    if {[$block findNet "${base}S0__BOT"] ne "NULL" ||
        [$block findNet "${base}S1__TOP"] ne "NULL" ||
        [$block findNet "${base}S2__BOT"] ne "NULL"} {
      incr rejected
      continue
    }
    if {![$net rename "${base}S0__BOT"]} {
      error "Cannot rename selected net $name"
    }
    set shared [odb::dbNet_create $block "${base}S1__TOP"]
    set terminal [odb::dbNet_create $block "${base}S2__BOT"]
    $shared setSigType SIGNAL
    $terminal setSigType SIGNAL
    $sink disconnect
    $sink connect $terminal
    add_hbt $block $bottom_master "LS_HBT_[expr {$selected * 2}]" \
      $source_site $net $shared
    add_hbt $block $top_master "LS_HBT_[expr {$selected * 2 + 1}]" \
      $sink_site $terminal $shared
    puts [format "TMLS selected %s -> %s score=%.3f paths=%d worst=%.4f span=%.1fum fanout=%d" \
      $name $sink_key $score $hits $slack $span_um $fanout]
    incr selected
  }
  puts "TMLS summary: candidates=[llength $candidates] selected=$selected rejected=$rejected report=$report_path"
}

tmls::prepare
