export type Locale = "en" | "th";

const dict = {
  en: {
    appName: "Singularity",
    tagline: "Design master data with a prompt.",
    signIn: "Sign in with Google",
    signOut: "Sign out",
    signedInAs: "Signed in as",
    role: "Role",
    canRead: "Read the schema and data",
    canEdit: "Edit master data",
    canApprove: "Approve schema changes",
    loading: "Loading…",
    forbiddenTitle: "Access denied",
    forbiddenBody: "Your account is not allowed to use Singularity. Ask an approver for access.",
    backHome: "Back to home",
    role_viewer: "Viewer",
    role_editor: "Editor",
    role_approver: "Approver",
  },
  th: {
    appName: "Singularity",
    tagline: "ออกแบบข้อมูลหลักด้วยการสั่งงาน",
    signIn: "เข้าสู่ระบบด้วย Google",
    signOut: "ออกจากระบบ",
    signedInAs: "เข้าสู่ระบบเป็น",
    role: "บทบาท",
    canRead: "ดูโครงสร้างและข้อมูล",
    canEdit: "แก้ไขข้อมูลหลัก",
    canApprove: "อนุมัติการเปลี่ยนโครงสร้าง",
    loading: "กำลังโหลด…",
    forbiddenTitle: "ไม่มีสิทธิ์เข้าใช้งาน",
    forbiddenBody: "บัญชีของคุณยังไม่ได้รับอนุญาตให้ใช้ Singularity โปรดติดต่อผู้อนุมัติ",
    backHome: "กลับหน้าแรก",
    role_viewer: "ผู้ดู",
    role_editor: "ผู้แก้ไข",
    role_approver: "ผู้อนุมัติ",
  },
} as const;

export type MessageKey = keyof (typeof dict)["en"];

export function t(locale: Locale, key: MessageKey): string {
  return dict[locale][key];
}
