import Link from "next/link";

export default function Home() {
  return (
    <main>
      <h1>Medical AI</h1>
      <p>Личный кабинет Medical AI</p>
      <Link href="/login">Вход</Link>{" "}<Link href="/register">Регистрация</Link>
    </main>
  );
}
