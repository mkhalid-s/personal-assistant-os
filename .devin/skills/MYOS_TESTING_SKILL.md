---
name: myos-test-validation
description: Run MYOS test suite with coverage analysis and resource warning detection
subagent: true
allowed-tools:
  - read
  - grep
  - glob
  - exec
  - write
permissions:
  allow:
    - Read(src/**)
    - Read(tests/**)
    - Exec(python -m unittest)
    - Exec(source .venv/bin/activate)
  deny:
    - Write(/etc/**)
---

Run comprehensive MYOS test validation:

1. **Environment Setup**
   - Activate the virtual environment
   - Set PYTHONPATH=src
   - Verify dependencies are installed

2. **Test Execution**
   - Run the full test suite: `python -m unittest discover -s tests -p "test_*.py"`
   - Capture test results and timing information
   - Monitor for resource warnings (database connections, file handles)

3. **Coverage Analysis**
   - Run coverage analysis if coverage.py is available
   - Identify code paths not covered by tests
   - Focus on safety-critical modules (execution.py, autonomy.py, privacy.py, db.py)

4. **Resource Warning Detection**
   - Check for unclosed database connections
   - Identify file handle leaks
   - Report any ResourceWarning output

5. **Results Reporting**
   - Summary of test results (pass/fail counts)
   - Execution time analysis
   - Resource warning details
   - Coverage gaps for critical modules
   - Recommendations for improvement

Pay special attention to:
- Safety-critical test failures
- Resource warnings that need fixing
- Coverage gaps in execution.py, autonomy.py, and privacy.py
- Any performance regressions compared to baseline