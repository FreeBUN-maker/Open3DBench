# Sourced only with GRT_FEEDBACK_TCL enabled, after the pass has routed.
if {[info commands grt::write_resources] eq ""} {
  error "MLSv2 resource interface missing: rebuild opt-code/baseline overlay"
}
set prefix $::env(RESULTS_DIR)/mls_${grt_feedback_die}
set fp [open ${prefix}.route_seconds w]
puts $fp $grt_feedback_route_seconds
close $fp
set feedback_start [clock milliseconds]
grt::write_resources ${prefix}.resources.tsv
write_global_route_segments ${prefix}.segments
estimate_parasitics -global_routing -spef_file ${prefix}.spef
if {![file exists ${prefix}.spef]} {
  error "MLSv2 requires one timing scene; per-scene SPEFs cannot be silently combined"
}
set fp [open ${prefix}.export_seconds w]
puts $fp [expr {([clock milliseconds]-$feedback_start)/1000.0}]
close $fp
