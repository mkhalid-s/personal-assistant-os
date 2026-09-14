---
name: myos-generate-tests
description: Generate tests for MYOS code changes focusing on edge cases and safety-critical paths
subagent: true
allowed-tools:
  - read
  - grep
  - glob
  - write
permissions:
  allow:
    - Read(src/**)
    - Read(tests/**)
    - Write(tests/**)
  deny:
    - Write(/etc/**)
---

Generate comprehensive tests for MYOS code changes:

1. **Code Analysis**
   - Analyze the target code file(s) to understand functionality
   - Identify function signatures, classes, and key logic paths
   - Note dependencies and external calls

2. **Test Strategy**
   - Identify existing tests for similar functionality
   - Determine test patterns used in the codebase
   - Focus on safety-critical paths and edge cases

3. **Test Generation**
   - Generate unit tests following the existing unittest pattern
   - Include test cases for:
     - Normal operation paths
     - Edge cases (empty inputs, boundary values)
     - Error conditions and exception handling
     - Resource cleanup (database connections, file handles)
   - For safety-critical code, add tests for:
     - Input validation and sanitization
     - Permission checks
     - Audit trail verification
     - Integrity checks

4. **Test Quality**
   - Ensure tests follow existing code style
   - Use proper setUp/tearDown methods
   - Include cleanup for database connections
   - Add descriptive test method names
   - Include docstrings explaining test purpose

5. **Integration**
   - Place tests in appropriate test file
   - Follow existing test class structure
   - Ensure tests can run independently

When generating tests for safety-critical modules (execution.py, autonomy.py, privacy.py, db.py):
- Always test failure modes
- Verify resource cleanup
- Test for potential security vulnerabilities
- Ensure audit trail integrity