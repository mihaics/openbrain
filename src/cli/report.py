"""
Report command for CLI.
"""
from argparse import Namespace

from ..analytics.weekly_report import generate_weekly_report


def report_cmd(args: Namespace) -> int:
    """Handle the report command."""
    days = args.days
    output_file = args.output
    
    print(f"Generating weekly report for last {days} days...")
    
    report = generate_weekly_report(days)
    
    if output_file:
        with open(output_file, 'w') as f:
            f.write(report)
        print(f"Report saved to {output_file}")
    else:
        print("\n" + "=" * 50)
        print("Weekly Report")
        print("=" * 50)
        print(report)
    
    return 0
