-- CHAPTER 7 HANDLING NULLS WITH JOIN:handle the nulls inside the keys 
-- SELECT a.year, a.type, a.orders, b.sales FROM table1 a JOIN table2 b ON a.year=b.year AND a.type=b.type 
-- here the join is the inner join and it's gonna give us only the matching resulls, year and type is the composite key 
-- sql cannot go and compare the nulls, which is a problem, which will be missing information and giving us inaccurate results
-- SELECT a.year, a.type, a.orders, b.sales FROM table1 a JOIN table2 b ON a.year=b.year AND COALESCE(a.type,'')=COALESCE(b.type ,'')-->usually go with empty strings which will be faster
-- handles the nulls before sorting the data
WITH     cte
AS       (SELECT 15 AS numero
          UNION
          SELECT 10 AS NUMERO
          UNION
          SELECT NULL AS NUMERO)
SELECT   *
FROM     cte
ORDER BY numero ASC;

-- sort the customers from lowest to highest scores, with nulls appearing last 
WITH     cte
AS       (SELECT 15 AS numero
          UNION
          SELECT 10 AS NUMERO
          UNION
          SELECT COALESCE (NULL, 999999999) AS NUMERO)
SELECT   *
FROM     cte
ORDER BY COALESCE (numero, 999999999) ASC; -- handling the nulls before sorting the data, LAZY, non professional, cause we are defining a static value

WITH     cte
AS       (SELECT 15 AS numero
          UNION
          SELECT 10 AS NUMERO
          UNION
          SELECT NULL AS NUMERO)
SELECT   *,
         CASE WHEN numero IS NULL THEN 1 ELSE 0 END AS score
FROM     cte
ORDER BY CASE WHEN numero IS NULL THEN 1 ELSE 0 END, numero;

-- here sort the data by flag once then sort the data by the number in ascending order
-- NULLIF:如果两个参数相等，就返回 NULL；如果不相等，就返回第一个参数
-- 1. 避免除以 0：计算比率时，分母可能是 0。把分母用 NULLIF 处理成 NULL
-- 2. 把「空字符串」当成缺失值：导入的数据里，有些字段是 '' 而不是真正的 NULL。用 NULLIF 把空字符串转成 NULL，后面的 COALESCE 或统计才能正确处理
-- 3. 把占位值变成 NULL：有些系统用 -1、'N/A' 或 0 表示「没有数据」。NULLIF 可以把这些占位值统一替换成 NULL，这样聚合函数 AVG、COUNT 就会忽略它们
-- NULLIF(value1,value2): NULLIF(shipping_address,'unknown')
-- 'unknown' 变回 NULL（用 NULLIF）：适合做计算和过滤，因为 NULL 在 SQL 里有特殊规则，AVG、COUNT 会自动忽略它，而 'unknown' 会被当成普通文字
-- ========================================================================================== --
-- Using the table cours_vocab in your app database, write a query that lists each word’s word and form. 
-- If form is the same as word, the second column should show NULL. If they are different, it should show form.
SELECT lexicon_id,
       word,
       form,
       NULLIF (word, form) AS word_form
FROM   cours_vocab
WHERE  NULLIF (word, form) IS NULL;