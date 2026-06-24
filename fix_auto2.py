with open('bin/auto', 'r') as f:
    text = f.read()

text = text.replace('AUTO_MODE="${AUTO_MODE:-friendly}"', 'AUTO_MODE="${AUTO_MODE:-dynamic}"')
text = text.replace('AUTO_MODE:-friendly', 'AUTO_MODE:-dynamic')
text = text.replace('AUTO_MODE=friendly', 'AUTO_MODE=dynamic')
text = text.replace('set_config_value AUTO_MODE friendly', 'set_config_value AUTO_MODE dynamic')

text = text.replace('[[ "${AUTO_MODE:-dynamic}" == "summer" ]] || return 0', 'return 0 # summer mode removed')

text = text.replace('''    case "${AUTO_MODE:-dynamic}" in
        calm)
            echo "Auto mode is set to Calm. Will rarely suggest Boost."
            ;;
        summer)
            echo "Auto mode is set to Summer. Thermals are capped to prevent room heating."
            ;;
        friendly)
            echo "Auto mode is set to Friendly (default)."
            ;;
        active)
            echo "Auto mode is set to Active. Very responsive."
            ;;
        quiet|off|dynamic) ;;
        quiet) echo "critical heat protection only" ;;
    esac''', '''    case "${AUTO_MODE:-dynamic}" in
        dynamic)
            echo "Auto mode is set to Dynamic (default)."
            ;;
        creator)
            echo "Auto mode is set to Creator. Maximizing thermal limits."
            ;;
        quiet)
            echo "Auto mode is set to Quiet. Critical heat protection only."
            ;;
        off) ;;
    esac''')

text = text.replace('''mode_description() {
    case "$1" in
        calm) echo "rare suggestions" ;;
        summer) echo "hot-room thermal headroom" ;;
        friendly) echo "balanced default" ;;
        active) echo "fast suggestions" ;;
        quiet) echo "critical heat protection only" ;;
        off) echo "disabled" ;;
        *) echo "$1" ;;
    esac
}''', '''mode_description() {
    case "$1" in
        dynamic) echo "balanced everyday" ;;
        creator) echo "gaming/rendering limits" ;;
        quiet) echo "strictly low noise/heat" ;;
        off) echo "disabled" ;;
        *) echo "$1" ;;
    esac
}''')


text = text.replace('''  auto on               enable calm auto mode
  auto off              disable auto suggestions
  auto calm             set calm mode
  auto summer           set summer mode for hot rooms
  auto friendly         set friendly mode
  auto active           set active mode
  auto quiet            set quiet mode
  auto mode calm        fewer suggestions, best for non-technical users
  auto mode summer      hot-room mode: cooler, quieter, slower to suggest Boost
  auto mode friendly    balanced suggestions, default
  auto mode active      more responsive suggestions
  auto mode quiet       only protect the PC when it gets too hot''', '''  auto on               enable dynamic auto mode
  auto off              disable auto mode
  auto mode dynamic     balanced suggestions, adapts to everyday workloads
  auto mode creator     gaming/rendering limits, prioritizes performance
  auto mode quiet       strict thermal/noise limits, best for meetings/library''')

text = text.replace('''show_config_friendly() {
    cat <<'EOF'
Configured Modes:
  calm      asks rarely; good if notifications annoy you
  summer    cooler hot-room behavior; avoids Boost when already warm
  friendly  balanced; good default
  active    reacts faster when gaming/rendering
  quiet     no suggestions, only safety changes when very hot
