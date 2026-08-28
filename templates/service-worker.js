// 캐시 이름 설정
const CACHE_NAME = 'woongstagram-v1';

// 설치 시 실행 (캐시 등록)
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // PWA 구동에 필요한 기본 파일 캐싱
      // "/"는 메인화면, 다른 정적 파일은 필요시 추가
      return cache.addAll(['/']);
    })
  );
});

// 활성화 시 실행 (이전 캐시 삭제)
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))
      );
    })
  );
});

// 네트워크 요청 시 실행 (캐시된 파일 사용 혹은 네트워크 요청)
self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => {
      // 캐시에 있으면 반환, 없으면 네트워크 요청
      return response || fetch(event.request);
    })
  );
});