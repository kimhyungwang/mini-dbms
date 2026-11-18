-- 미니 DBMS 예제
-- 실행: python main.py examples/sample.sql

INSERT INTO users VALUES ('철수', 20, 'chulsoo@mail.com');
INSERT INTO users (name, age) VALUES ('영희', 22), ('민수', 17);
INSERT INTO products VALUES ('키보드', 50000), ('마우스', 30000);

SELECT * FROM users;
SELECT name, age FROM users WHERE age >= 18;
SELECT * FROM users WHERE id = 2;
SELECT * FROM products WHERE price < 40000;
