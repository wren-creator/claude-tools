"""Tests for jcl_lint_bridge. Run: .venv/bin/python -m unittest jcl_lint_test"""
import unittest

from jcl_lint_bridge import _lint

GOOD = """\
//MYJOB    JOB (ACCT),'BRITLEY',CLASS=A,MSGCLASS=X,NOTIFY=&SYSUID
//* a comment
//STEP1    EXEC PGM=IEFBR14
//DD1      DD DSN=MY.TEST.DATA,DISP=(NEW,CATLG,DELETE),
//            SPACE=(TRK,(5,5)),DCB=(RECFM=FB,LRECL=80,BLKSIZE=800)
//STEP2    EXEC PGM=IEBGENER
//SYSUT1   DD DSN=MY.TEST.DATA,DISP=SHR
//SYSUT2   DD SYSOUT=*
//SYSIN    DD DUMMY
//STEP3    EXEC PGM=SORT
//SYSIN    DD *
  SORT FIELDS=(1,5,CH,A)
/*
//SORTIN   DD DSN=*.STEP2.SYSUT1,DISP=SHR
//
"""


def msgs(jcl):
    return [(ln, sev, m) for ln, sev, m in _lint(jcl)[0]]


def has(jcl, text, sev=None):
    return any(text in m and (sev is None or s == sev) for _, s, m in _lint(jcl)[0])


class JclLintTest(unittest.TestCase):
    def test_good_job_is_clean(self):
        self.assertEqual(msgs(GOOD), [])

    def test_instream_data_is_not_linted(self):
        jcl = GOOD.replace("  SORT FIELDS=(1,5,CH,A)", "this is lower case data // not jcl")
        self.assertEqual(msgs(jcl), [])

    def test_missing_job(self):
        self.assertTrue(has("//STEP1 EXEC PGM=IEFBR14\n", "first statement must be JOB"))

    def test_job_without_exec(self):
        self.assertTrue(has("//J JOB (A),'X'\n", "no EXEC"))

    def test_dd_before_exec(self):
        self.assertTrue(has("//J JOB (A),'X'\n//SYSIN DD DUMMY\n//S EXEC PGM=A\n", "before the first EXEC"))

    def test_joblib_before_exec_ok(self):
        self.assertFalse(has("//J JOB (A),'X'\n//JOBLIB DD DSN=A.B,DISP=SHR\n//S EXEC PGM=A\n", "before the first EXEC"))

    def test_name_too_long(self):
        self.assertTrue(has("//TOOLONGNAME JOB (A),'X'\n//S EXEC PGM=A\n", "longer than 8"))

    def test_lowercase(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC pgm=iefbr14\n", "lowercase"))

    def test_lowercase_in_quotes_ok(self):
        self.assertFalse(has("//J JOB (A),'brit hoff'\n//S EXEC PGM=A\n", "lowercase"))

    def test_unknown_operation(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXCE PGM=A\n", "did you mean EXEC"))

    def test_misspelled_dd_keyword(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,DISPP=SHR\n", "did you mean DISP"))

    def test_duplicate_step(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A\n//S EXEC PGM=B\n", "duplicate step name"))

    def test_duplicate_dd_in_step_but_not_across_steps(self):
        jcl = "//J JOB (A),'X'\n//S1 EXEC PGM=A\n//D DD DUMMY\n//D DD DUMMY\n//S2 EXEC PGM=A\n//D DD DUMMY\n"
        dup = [m for _, _, m in _lint(jcl)[0] if "duplicate DD" in m]
        self.assertEqual(len(dup), 1)

    def test_dd_concatenation_allowed(self):
        jcl = "//J JOB (A),'X'\n//S EXEC PGM=A\n//LIB DD DSN=A.B,DISP=SHR\n//   DD DSN=C.D,DISP=SHR\n//   DD DSN=E.F,DISP=SHR\n"
        self.assertEqual(msgs(jcl), [])

    def test_exec_without_pgm(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC REGION=0M\n", "needs PGM="))

    def test_proc_call_not_keyword_checked(self):
        self.assertEqual(msgs("//J JOB (A),'X'\n//S EXEC MYPROC,HLQ=PROD,PARM.GO='X'\n"), [])

    def test_broken_continuation(self):
        jcl = "//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,\n//E DD DUMMY\n"
        self.assertTrue(has(jcl, "starts a new statement"))

    def test_comma_then_eof(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A,\n", "not a continuation"))

    def test_continuation_column(self):
        jcl = "//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,\n//                        DISP=SHR\n"
        self.assertTrue(has(jcl, "columns 4 to 16"))

    def test_comment_between_continuations(self):
        jcl = "//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,\n//* note\n//   DISP=SHR\n"
        self.assertEqual(msgs(jcl), [])

    def test_long_record(self):
        self.assertTrue(has("//J JOB (A),'X'" + " " * 70 + "TOOLONG\n//S EXEC PGM=A\n", "80"))

    def test_bad_dsn(self):
        for dsn in ("TOOLONGQUALIFIER.A", "A..B", "1ABC.DEF", "A." + "B" * 9):
            jcl = f"//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN={dsn},DISP=SHR\n"
            self.assertTrue(has(jcl, "", sev="ERROR"), dsn)

    def test_dsn_forms_ok(self):
        for dsn in ("MY.PDS(MEMBER)", "MY.GDG(+1)", "MY.GDG(0)", "&&TEMP", "&HLQ..DATA", "A-B.C"):
            jcl = f"//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN={dsn},DISP=SHR\n"
            self.assertEqual(msgs(jcl), [], dsn)

    def test_dsn_too_long(self):
        dsn = ".".join(["ABCDEFGH"] * 6)
        self.assertTrue(has(f"//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN={dsn},DISP=SHR\n", "max is 44"))

    def test_bad_disp(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,DISP=(OLDD,KEEP)\n", "status 'OLDD'"))
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,DISP=(OLD,KEEPP)\n", "normal disposition"))

    def test_missing_disp(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B\n", "no DISP", "WARN"))

    def test_new_without_space(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,DISP=(NEW,CATLG)\n", "no SPACE"))

    def test_new_deleted_at_step_end(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,DISP=(NEW),SPACE=(TRK,1)\n", "deleted when the step ends"))

    def test_blksize_lrecl(self):
        jcl = "//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,DISP=SHR,DCB=(RECFM=FB,LRECL=80,BLKSIZE=801)\n"
        self.assertTrue(has(jcl, "not a multiple"))
        jcl = "//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD DSN=A.B,DISP=SHR,DCB=(RECFM=VB,LRECL=100,BLKSIZE=100)\n"
        self.assertTrue(has(jcl, "less than LRECL+4"))

    def test_bad_referback(self):
        jcl = "//J JOB (A),'X'\n//S1 EXEC PGM=A\n//D DD DSN=*.NOSTEP.SYSUT2,DISP=SHR\n"
        self.assertTrue(has(jcl, "NOSTEP"))

    def test_sysout_star_not_a_referback(self):
        self.assertEqual(msgs("//J JOB (A),'X'\n//S EXEC PGM=A\n//D DD SYSOUT=*\n"), [])

    def test_if_balance(self):
        base = "//J JOB (A),'X'\n//S EXEC PGM=A\n"
        self.assertTrue(has(base + "//I IF (S.RC = 0) THEN\n//T EXEC PGM=B\n", "never closed"))
        self.assertTrue(has(base + "// ENDIF\n", "ENDIF with no open IF"))
        self.assertEqual(msgs(base + "//I IF (S.RC = 0) THEN\n//T EXEC PGM=B\n// ELSE\n//U EXEC PGM=C\n// ENDIF\n"), [])

    def test_password_on_job(self):
        self.assertTrue(has("//J JOB (A),'X',PASSWORD=SECRET\n//S EXEC PGM=A\n", "credential"))

    def test_parm_length(self):
        jcl = ("//J JOB (A),'X'\n//S EXEC PGM=A,PARM='" + "X" * 51 + "\n//" + " " * 13 + "X" * 50 + "'\n")
        self.assertTrue(has(jcl, "max is 100"))

    def test_unbalanced_quote(self):
        self.assertTrue(has("//J JOB (A),'X\n//S EXEC PGM=A\n", "", sev="ERROR"))

    def test_garbage_record(self):
        self.assertTrue(has("//J JOB (A),'X'\n//S EXEC PGM=A\nSOMETHING ELSE\n", "does not start with //"))

    def test_multiple_jobs(self):
        jcl = "//J1 JOB (A),'X'\n//S EXEC PGM=A\n//J2 JOB (A),'X'\n//S EXEC PGM=A\n"
        self.assertEqual(msgs(jcl), [])

    def test_cataloged_proc_source(self):
        jcl = "//MYPROC PROC HLQ=PROD\n//S1 EXEC PGM=A\n//D DD DSN=&HLQ..DATA,DISP=SHR\n// PEND\n"
        self.assertEqual(msgs(jcl), [])


if __name__ == "__main__":
    unittest.main()
